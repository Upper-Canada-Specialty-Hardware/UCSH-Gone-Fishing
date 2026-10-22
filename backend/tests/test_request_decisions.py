"""The one decision path, tested per kind, channel and outcome (#322, #324-#327).

These call ``request_decisions.approve`` / ``reject`` / ``refund`` directly (the
service-layer gate that routes to them is covered elsewhere), with an in-memory
``StaffBalances`` in place of SharePoint or Postgres staff, so the whole decision
runs against the SQLite test database and never touches a real backend. Each test
checks the balance move, the ledger rows, the timeline events, the request status
and the queued result message together, because the point of this path is that
they all land in one transaction.
"""
import asyncio
import itertools
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.repositories.staff_balances import POT_COLUMNS
from app.services import request_decisions as rd

# Fresh, unique ids each insert: a unique item_id per kind, and a unique employee
# id so the per-employee lock never carries between tests (and between loops).
_item_ids = itertools.count(50_000_000)
_employee_ids = itertools.count(900_000)


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


class FakeStaffBalances:
    """An in-memory StaffBalances: holds pots in a dict, records every write."""

    def __init__(self, pots: dict[str, float]):
        """Start with a full set of pots.

        Args:
            pots: ``{pot: value}`` for all five stored pots.
        """
        self._pots = dict(pots)                                    # the current pots
        self.write_calls: list[dict] = []                         # every write_pots payload, in order

    async def lock_pots(self, session, employee_id):
        """Return a copy of the current pots (no real lock needed in tests).

        Args:
            session: Ignored.
            employee_id: Ignored.

        Returns:
            A copy of the pots dict.
        """
        return dict(self._pots)

    async def write_pots(self, session, employee_id, new_values):
        """Apply a pot write and remember it.

        Args:
            session: Ignored.
            employee_id: Ignored.
            new_values: ``{pot: new_value}`` to set.
        """
        self.write_calls.append(dict(new_values))
        self._pots.update(new_values)


def _all_pots(**overrides) -> dict[str, float]:
    """A full set of pots at zero, with the named ones overridden.

    Args:
        **overrides: ``pot=value`` for any pot to start non-zero.

    Returns:
        ``{pot: value}`` for every stored pot.
    """
    pots = {pot: 0.0 for pot in POT_COLUMNS}                       # sick, make_up, carry_over, vacation, payout
    pots.update(overrides)
    return pots


def _use(monkeypatch, pots: dict[str, float]) -> FakeStaffBalances:
    """Install a fake StaffBalances the decision path will use, and return it.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        pots: The starting pots for the fake.

    Returns:
        The FakeStaffBalances, so a test can read its writes.
    """
    fake = FakeStaffBalances(pots)

    async def _get():
        return fake

    monkeypatch.setattr(rd, "get_staff_balances", _get)           # the decision path reads this
    return fake


async def _insert(**fields) -> RequestRecord:
    """Insert one pending request with sensible defaults, and return it.

    Args:
        **fields: Column overrides (kind, item_id, employee fields, details).

    Returns:
        The committed RequestRecord (detached, read for its ids).
    """
    base = dict(
        kind="leave",
        item_id=next(_item_ids),
        status="pending",
        employee_id=str(next(_employee_ids)),
        employee_name="Pat Staff",
        employee_email="pat@ucsh.com",
        manager_name="Boss Person",
        employment_type="Salary",
        approval_version=1,
        balance_state="not_applied",
    )
    base.update(fields)
    async with async_session() as session:
        record = RequestRecord(**base)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


def _leave(**fields) -> RequestRecord:
    """Insert a pending leave request (needs a leave_type and start_date)."""
    fields.setdefault("leave_type", "Sick or Personal Day")
    fields.setdefault("start_date", date(2026, 11, 2))
    return run(_insert(kind="leave", **fields))


def _overtime(**fields) -> RequestRecord:
    """Insert a pending overtime request (needs a date worked and hours)."""
    fields.setdefault("start_date", date(2026, 11, 2))
    fields.setdefault("hours", 16.0)
    return run(_insert(kind="overtime", **fields))


def _carryover(**fields) -> RequestRecord:
    """Insert a pending carry-over/payout request (needs an option and days)."""
    fields.setdefault("request_option", "carry_over")
    fields.setdefault("days", 3.0)
    return run(_insert(kind="carryover_payout", **fields))


async def _inspect(record: RequestRecord):
    """Read back a request, its ledger rows, its events and its outbox rows.

    Args:
        record: The request to re-read (by its id).

    Returns:
        ``(fresh_record, ledger_rows, events, outbox_rows)``.
    """
    async with async_session() as session:
        fresh = await session.get(RequestRecord, record.id)
        ledger = (await session.execute(
            select(BalanceLedger).where(BalanceLedger.request_id == record.id)
        )).scalars().all()
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == record.id)
        )).scalars().all()
        outbox = (await session.execute(
            select(Outbox).where(Outbox.request_id == record.id)
        )).scalars().all()
        return fresh, ledger, events, outbox


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the request tables before and after each test, for isolation."""
    async def wipe():
        async with async_session() as session:
            for model in (BalanceLedger, RequestEvent, Outbox, RequestChange,
                          RequestRecord, RequestCounter):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


# ----- approve: the balance moves, per kind -----


def test_salaried_sick_leave_moves_the_sick_pot_and_queues_the_result(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    result = run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))

    assert result["status"] == "approved"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "approved" and fresh.balance_state == "applied"
    assert fresh.decided_channel == "email_link"
    # The sick pot moved 5 -> 3, written once and recorded once.
    assert fake.write_calls == [{"sick": 3.0}]
    sick_rows = [row for row in ledger if row.pot == "sick"]
    assert len(sick_rows) == 1 and sick_rows[0].before == 5.0 and sick_rows[0].after == 3.0
    types_seen = {event.type for event in events}
    assert {"approved", "balance_applied"} <= types_seen
    # One result email is queued to the employee, carrying the balance line.
    assert len(outbox) == 1 and outbox[0].channel == "email"
    assert outbox[0].recipient == "pat@ucsh.com" and outbox[0].status == "pending"
    assert "3 days" in outbox[0].body


def test_salaried_vacation_leave_spends_make_up_first(monkeypatch):
    fake = _use(monkeypatch, _all_pots(make_up=4, vacation=10))
    record = _leave(leave_type="Vacation", days=3)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"
    # Vacation leave spends Make-Up first: 4 -> 1, vacation untouched.
    assert fake.write_calls == [{"make_up": 1.0}]


def test_overtime_banks_make_up_for_salaried(monkeypatch):
    fake = _use(monkeypatch, _all_pots(make_up=0, vacation=0))
    record = _overtime(hours=16.0)

    result = run(rd.approve("overtime", record.item_id, "Boss", "sms"))

    assert result["status"] == "approved"
    # 16 hours / 8 = 2 days added to Make-Up.
    assert fake.write_calls == [{"make_up": 2.0}]


def test_carryover_moves_vacation_into_carry_over(monkeypatch):
    fake = _use(monkeypatch, _all_pots(vacation=10))
    record = _carryover(request_option="carry_over", days=3.0)

    result = run(rd.approve("carryover_payout", record.item_id, "Boss", "admin_dashboard"))

    assert result["status"] == "approved"
    # Three days leave vacation (10 -> 7) and land in carry-over (0 -> 3).
    assert fake.write_calls[0] == {"vacation": 7.0, "carry_over": 3.0}


def test_a_payout_that_would_go_below_zero_is_refused_and_writes_no_balance(monkeypatch):
    fake = _use(monkeypatch, _all_pots(vacation=10))
    record = _carryover(request_option="payout", days=20.0)

    result = run(rd.approve("carryover_payout", record.item_id, "Boss", "admin_dashboard"))

    assert "error" in result and result["refused"] == "negative"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "pending"                              # nothing decided
    assert fake.write_calls == [] and ledger == []               # no balance moved
    assert any(event.type == "refused" for event in events)
    assert outbox == []                                          # no result message


# ----- approve: a clash with an approved request is refused -----


def test_a_leave_that_overlaps_an_approved_leave_is_refused(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5))
    approved = _leave(status="approved", start_date=date(2026, 11, 2), end_date=date(2026, 11, 4), days=3)
    record = _leave(employee_id=approved.employee_id, start_date=date(2026, 11, 3), days=1)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["refused"] == "conflict"
    assert f"#{approved.item_id}" in result["error"]              # names the approved request
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "pending"                              # stays for the manager to decide
    assert fake.write_calls == [] and ledger == [] and outbox == []
    assert any(event.type == "refused" for event in events)


def test_half_days_that_fit_on_one_date_are_both_approved(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    approved = _leave(status="approved", start_date=date(2026, 11, 2), days=0.5)
    record = _leave(employee_id=approved.employee_id, start_date=date(2026, 11, 2), days=0.5)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"                         # two halves make one full day


def test_another_persons_approved_leave_is_not_a_clash(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    _leave(status="approved", start_date=date(2026, 11, 2), days=1)  # a different employee id
    record = _leave(start_date=date(2026, 11, 2), days=1)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"


def test_overtime_on_an_already_approved_date_is_refused(monkeypatch):
    fake = _use(monkeypatch, _all_pots())
    approved = _overtime(status="approved", start_date=date(2026, 11, 2))
    record = _overtime(employee_id=approved.employee_id, start_date=date(2026, 11, 2))

    result = run(rd.approve("overtime", record.item_id, "Boss", "sms"))

    assert result["refused"] == "conflict" and fake.write_calls == []


def test_no_cost_leave_approves_without_touching_any_balance(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Bereavement", days=2)

    result = run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))

    assert result["status"] == "approved"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.balance_state == "no_balance"
    assert fake.write_calls == [] and ledger == []
    assert any(event.type == "no_balance_used" for event in events)
    assert len(outbox) == 1                                       # the employee is still told


def test_work_from_home_moves_nothing_even_with_a_negative_carry_over(monkeypatch):
    """#375: a negative carry-over (left by a next-year vacation) stays put."""
    fake = _use(monkeypatch, _all_pots(sick=5, carry_over=-10, vacation=6))
    record = _leave(leave_type="Work from Home", days=1)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"
    fresh, ledger, _events, _outbox = run(_inspect(record))
    assert fresh.balance_state == "no_balance"                    # no overflow step ran
    assert fake.write_calls == [] and ledger == []                # vacation still 6, carry-over still -10


def test_hourly_work_from_home_is_not_counted_as_vacation(monkeypatch):
    """#375: hourly work from home adds nothing to vacation taken."""
    fake = _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Work from Home", days=2, employment_type="Hourly")

    run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    fresh, ledger, _events, _outbox = run(_inspect(record))
    assert fresh.balance_state == "no_balance"                    # not "counted"
    assert fake.write_calls == [] and ledger == []                # no vacation_taken row


def test_work_from_home_has_no_simulated_impact():
    """#375: the shared simulator (SharePoint path and previews) returns no impact."""
    from app.services.balance import simulate_leave_impact
    from app.services.employment import leave_costs_balance

    fields = {"SalaryHourly": "Salary", "CarryOver": -10, "CurrentVacationBalance": 6}
    assert simulate_leave_impact(fields, "Work from Home", 1, carryover_available=True) is None
    assert leave_costs_balance(fields, "Work from Home") is False   # the SharePoint approval returns early


# ----- approve: the hourly rules -----


def test_hourly_sick_takes_whole_sick_days(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Sick or Personal Day", days=2, employment_type="Hourly")

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"
    assert fake.write_calls == [{"sick": 3.0}]                    # sick only, no cascade


def test_hourly_vacation_is_counted_not_spent(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Vacation", days=2, employment_type="Hourly BC")

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved" and result["balance_state"] == "counted"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fake.write_calls == []                                 # no pot moves for hourly vacation
    counter_rows = [row for row in ledger if row.pot == "vacation_taken"]
    assert len(counter_rows) == 1 and counter_rows[0].after == 2.0
    assert any(event.type == "counted" for event in events)


def test_hourly_overtime_banks_nothing(monkeypatch):
    fake = _use(monkeypatch, _all_pots())
    record = _overtime(hours=8.0, employment_type="Hourly")

    result = run(rd.approve("overtime", record.item_id, "Boss", "sms"))

    assert result["status"] == "approved"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.balance_state == "no_balance"
    assert fake.write_calls == [] and ledger == []


# ----- reject -----


def test_reject_queues_a_rejection_and_moves_no_balance(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    result = run(rd.reject("leave", record.item_id, "Boss", "email_link",
                           link_version=1, reason="Not this week."))

    assert result["status"] == "rejected"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "rejected"
    assert fake.write_calls == [] and ledger == []
    assert any(event.type == "rejected" for event in events)
    assert len(outbox) == 1 and "Not this week." in outbox[0].body


# ----- exactly-once across channels, and stale links -----


def test_a_second_decision_from_another_channel_is_refused(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))
    second = run(rd.approve("leave", record.item_id, "Boss", "sms"))

    assert second["refused"] == "decided"
    fresh, ledger, events, outbox = run(_inspect(record))
    # The balance moved exactly once despite the second attempt.
    sick_rows = [row for row in ledger if row.pot == "sick"]
    assert len(sick_rows) == 1
    assert any(event.type == "refused" for event in events)


def test_a_stale_link_version_is_refused(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2, approval_version=2)

    # The link carries v1 but the request is at v2: an admin edit bumped it.
    result = run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))

    assert result["refused"] == "stale"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "pending" and ledger == []


# ----- the idempotency guard: a committed ledger key is not re-applied -----


def test_a_committed_ledger_key_does_not_move_the_balance_again(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    async def seed():
        # A prior attempt's ledger row already carries this step's key, standing
        # in for a crash after the ledger committed but before status was set.
        key = rd.step_key("leave", record.item_id, "ledger:sick", 1)
        async with async_session() as session:
            session.add(BalanceLedger(
                request_id=record.id, employee_id=record.employee_id, pot="sick",
                change=-2.0, before=5.0, after=3.0, reason="prior", actor="Boss",
                channel="email_link", idempotency_key=key,
            ))
            await session.commit()

    run(seed())
    result = run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))

    assert result["status"] == "approved"
    # The guard saw the key, so SharePoint/Postgres is not written a second time.
    assert fake.write_calls == []


# ----- refund reverses exactly what the approval moved -----


def test_refund_restores_the_pot_and_records_the_reversal(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Sick or Personal Day", days=2)
    run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))
    assert fake.write_calls == [{"sick": 3.0}]

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "refunded" and fresh.balance_state == "reversed"
    # The pot is put back 3 -> 5, undoing the approval exactly.
    assert fake.write_calls[-1] == {"sick": 5.0}
    assert any(event.type == "balance_reversed" for event in events)
    reversal = [row for row in ledger if row.pot == "sick" and row.change > 0]
    assert len(reversal) == 1 and reversal[0].after == 5.0


def test_refund_reverses_an_hourly_vacation_counter(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Vacation", days=2, employment_type="Hourly")
    run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    fresh, ledger, events, outbox = run(_inspect(record))
    counter_rows = [row for row in ledger if row.pot == "vacation_taken"]
    # One row added +2, one reversal row brought it back to 0.
    assert len(counter_rows) == 2
    assert counter_rows[-1].after == 0.0


def test_refund_requires_an_approved_request(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert "error" in result and "approved" in result["error"].lower()


# ----- legacy refunds: re-derive the balance effect when there is no ledger (finding 8) -----


def test_legacy_refund_reconstructs_a_salaried_vacation_with_no_ledger(monkeypatch):
    fake = _use(monkeypatch, _all_pots(make_up=0, vacation=10))
    # Approved, but with no ledger rows: a leave approved in SharePoint before the
    # ledger existed. The refund must re-derive what it moved and give it back.
    record = _leave(leave_type="Vacation", days=3, status="approved", balance_state="legacy_unknown")

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded" and result["no_balance_change"] is False
    fresh, ledger, events, outbox = run(_inspect(record))
    # Vacation leave spends Make-Up first, so the refund restores 3 days to Make-Up.
    assert fake.write_calls == [{"make_up": 3.0}]
    make_up_rows = [row for row in ledger if row.pot == "make_up"]
    assert len(make_up_rows) == 1 and make_up_rows[0].after == 3.0
    assert "reconstructed" in (make_up_rows[0].reason or "")    # the ledger says the amount was rebuilt
    assert any(e.type == "balance_reversed" for e in events)
    assert outbox and "3 days" in outbox[0].body                # the email shows the restored amount


def test_legacy_refund_reconstructs_an_hourly_vacation_counter(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Vacation", days=2, employment_type="Hourly",
                    status="approved", balance_state="legacy_unknown")

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded" and result["no_balance_change"] is False
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fake.write_calls == []                               # hourly vacation is a counter, no pot moves
    counter = [row for row in ledger if row.pot == "vacation_taken"]
    # No prior counter row, so reversing the +2 the approval counted lands at -2.
    assert len(counter) == 1 and counter[0].after == -2.0
    assert "reconstructed" in (counter[0].reason or "")


def test_legacy_refund_of_a_no_cost_leave_restores_nothing(monkeypatch):
    fake = _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Bereavement", days=2, status="approved", balance_state="legacy_unknown")

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded" and result["no_balance_change"] is True
    assert fake.write_calls == []                               # bereavement never cost anything


# ----- the Postgres decision runs the SharePoint path's follow-ups (finding 11) -----


def test_approve_recalcs_the_allow_date_and_sends_the_blocked_notice(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Sick or Personal Day", days=2)
    recalc_calls: list = []
    blocked_calls: list = []

    async def fake_recalc(employee_id, vacation, carryover):
        recalc_calls.append((str(employee_id), vacation, carryover))
        return None

    async def fake_blocked(kind, item_id, fields, email):
        blocked_calls.append((kind, item_id, email))
        return 0

    monkeypatch.setattr("app.services.balance.recalculate_request_allow_date", fake_recalc)
    monkeypatch.setattr("app.services.notify_blocked.notify_requests_blocked_by_approval", fake_blocked)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["status"] == "approved"
    assert len(recalc_calls) == 1 and recalc_calls[0][0] == record.employee_id
    assert len(blocked_calls) == 1 and blocked_calls[0][0] == "leave"


def test_refund_recalcs_the_allow_date_but_sends_no_blocked_notice(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5, vacation=10))
    record = _leave(leave_type="Sick or Personal Day", days=2)
    run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))
    recalc = {"n": 0}
    blocked = {"n": 0}

    async def fake_recalc(*_a):
        recalc["n"] += 1
        return None

    async def fake_blocked(*_a):
        blocked["n"] += 1
        return 0

    monkeypatch.setattr("app.services.balance.recalculate_request_allow_date", fake_recalc)
    monkeypatch.setattr("app.services.notify_blocked.notify_requests_blocked_by_approval", fake_blocked)

    run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert recalc["n"] == 1 and blocked["n"] == 0               # a refund frees dates, strands nothing


# ----- SharePoint is never touched by a Postgres decision -----


def test_a_postgres_decision_never_calls_sharepoint(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    from app.graph import sharepoint

    async def boom(*args, **kwargs):
        raise AssertionError("the decision path reached SharePoint")

    for name in ("get_list_item", "get_list_item_or_none", "create_list_item",
                 "update_list_item_fields"):
        monkeypatch.setattr(sharepoint.sp_client, name, boom)

    result = run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))
    assert result["status"] == "approved"                        # no SharePoint call raised


# ----- a late link-open does not undo a decided stage (#323) -----


def test_a_late_link_open_keeps_the_decided_stage(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)
    run(rd.approve("leave", record.item_id, "Boss", "email_link", link_version=1))

    run(rd.record_link_opened("leave", record.item_id, 1, "email_link", "Boss"))

    fresh, ledger, events, outbox = run(_inspect(record))
    # The stage still names the outcome, not "Manager opened the link".
    assert fresh.stage == "Approved, balance updated"
    assert any(event.type == "link_opened" for event in events)


# ----- refunding banked overtime that was already spent cascades (finding 6) -----


def test_refunding_spent_overtime_cascades_so_no_pot_sits_negative(monkeypatch):
    # One employee: bank 8h overtime (Make-Up 0 -> 1), spend it on a vacation day
    # (Make-Up 1 -> 0), then refund the overtime. Reversing the +1 Make-Up alone
    # would leave Make-Up at -1; the refund must cascade that deficit down the
    # chain (Make-Up -> Carry Over -> Vacation) so no pot but Vacation sits below
    # zero, exactly as an approval's own spend cascades.
    emp = str(next(_employee_ids))
    fake = _use(monkeypatch, _all_pots())                         # every pot starts at zero
    overtime = _overtime(hours=8.0, employee_id=emp)
    run(rd.approve("overtime", overtime.item_id, "Boss", "manager_dashboard"))
    assert fake._pots["make_up"] == 1.0                           # 8h / 8 banked into Make-Up

    vacation = _leave(leave_type="Vacation", days=1.0, employee_id=emp)
    run(rd.approve("leave", vacation.item_id, "Boss", "manager_dashboard"))
    assert fake._pots["make_up"] == 0.0                           # the vacation spent Make-Up first

    result = run(rd.refund("overtime", overtime.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    # Make-Up and Carry Over never sit negative: the deficit lands on Vacation.
    assert fake._pots["make_up"] == 0.0
    assert fake._pots["carry_over"] == 0.0
    assert fake._pots["vacation"] == -1.0
    _fresh, ledger, _events, _outbox = run(_inspect(overtime))
    cascade_rows = [row for row in ledger if "cascade" in (row.reason or "")]
    assert any(row.pot == "vacation" and row.after == -1.0 for row in cascade_rows)


# ----- the vacation booking limit is enforced at approval (#407) -----


def test_approving_a_vacation_past_the_booking_limit_is_refused_and_stays_pending(monkeypatch):
    # A salaried person with vacation left may book until Mar 31 next year; a June
    # next-year vacation is past that, so approval is refused and nothing is
    # written. The request stays Pending for the manager to decide.
    fake = _use(monkeypatch, _all_pots(vacation=10))              # V>0, C==0 -> limit Mar 31 next year
    next_june = date(date.today().year + 1, 6, 1)
    record = _leave(leave_type="Vacation", days=1, start_date=next_june, end_date=next_june)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result["refused"] == "booking_limit"
    assert "book vacation until" in result["error"]
    fresh, ledger, _events, _outbox = run(_inspect(record))
    assert fresh.status == "pending"                             # untouched: the manager still decides
    assert fake.write_calls == [] and ledger == []               # no balance moved


def test_refund_of_an_old_next_year_vacation_restores_make_up(monkeypatch):
    # A vacation approved under the old next-year path has no ledger rows; its
    # refund reconstructs that the day came off Make-Up and gives it back, then
    # the normal cascade runs. Make-Up returns to where it started.
    fake = _use(monkeypatch, _all_pots(make_up=0, vacation=10))
    next_june = date(date.today().year + 1, 6, 1)
    record = _leave(leave_type="Vacation", days=1, status="approved",
                    balance_state="legacy_unknown", start_date=next_june, end_date=next_june)

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    assert fake._pots["make_up"] == 1.0                          # the vacation day returned to Make-Up
    assert fake._pots["carry_over"] == 0.0                       # carry-over never left zero


# ----- refunding a carry-over spend that went below zero splits the restore (#407) -----


async def _add_approval_ledger(record, pot, before, after):
    """Insert one approval ledger row for a request, for the refund-split tests.

    The new cascade never drives carry-over below zero, so a row like carry-over
    0 -> -5 can only come from an old next-year approval. These tests write that
    historical row directly rather than reproducing the retired cascade.

    Args:
        record: The approved request the row belongs to.
        pot: Which pot moved.
        before: The pot value before the approval.
        after: The pot value after the approval.
    """
    async with async_session() as session:
        session.add(BalanceLedger(
            request_id=record.id, employee_id=record.employee_id, pot=pot,
            change=after - before, before=before, after=after,
            reason=f"approved {record.kind} #{record.item_id}", actor="Boss",
            channel="manager_dashboard", idempotency_key=f"appr:{record.id}:{pot}",
        ))
        await session.commit()


def test_refund_of_a_fixed_negative_carry_over_restores_vacation_not_carry_over(monkeypatch):
    # Dexter: an old approval drove carry-over 0 -> -5 (its ledger row), then the
    # setup fix moved that -5 onto vacation (now C 0, V 9). Refunding must give the
    # 5 back to VACATION, not recreate carry-over the fix already cleared.
    emp = str(next(_employee_ids))
    fake = _use(monkeypatch, _all_pots(carry_over=0.0, vacation=9.0))
    record = _leave(leave_type="Sick or Personal Day", days=1, status="approved",
                    balance_state="applied", employee_id=emp)
    run(_add_approval_ledger(record, "carry_over", 0.0, -5.0))

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    assert fake._pots["carry_over"] == 0.0                       # not recreated
    assert fake._pots["vacation"] == 14.0                        # the 5 went back to vacation


def test_refund_of_an_unfixed_negative_carry_over_clears_the_shortfall(monkeypatch):
    # The same old approval (carry-over 0 -> -5), but the setup fix was never
    # applied, so carry-over still sits at -5. Refunding brings it back to 0;
    # vacation is left where it is (the borrowed 5 just clears the shortfall).
    emp = str(next(_employee_ids))
    fake = _use(monkeypatch, _all_pots(carry_over=-5.0, vacation=14.0))
    record = _leave(leave_type="Sick or Personal Day", days=1, status="approved",
                    balance_state="applied", employee_id=emp)
    run(_add_approval_ledger(record, "carry_over", 0.0, -5.0))

    run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert fake._pots["carry_over"] == 0.0                       # shortfall cleared
    assert fake._pots["vacation"] == 14.0                        # vacation untouched


def test_refund_of_a_real_carry_over_spend_restores_carry_over(monkeypatch):
    # A post-#407 request that spent real carry-over (3 -> 0): the whole 3 goes
    # straight back to carry-over, since none of it went below zero, and vacation
    # is untouched.
    emp = str(next(_employee_ids))
    fake = _use(monkeypatch, _all_pots(carry_over=0.0, vacation=10.0))
    record = _leave(leave_type="Sick or Personal Day", days=1, status="approved",
                    balance_state="applied", employee_id=emp)
    run(_add_approval_ledger(record, "carry_over", 3.0, 0.0))

    run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert fake._pots["carry_over"] == 3.0                       # real carry-over put back
    assert fake._pots["vacation"] == 10.0                        # vacation untouched


# ----- an unmatched employee is refused cleanly, not a 500 (finding 9) -----


class _NoStaffBalances:
    """A StaffBalances whose lock_pots always raises, as for an unmatched employee."""

    async def lock_pots(self, session, employee_id):
        """Raise as PostgresStaffBalances does when there is no staff row."""
        raise LookupError(f"no staff row for {employee_id}")

    async def write_pots(self, session, employee_id, new_values):
        """Never reached: a missing employee stops before any write."""
        raise AssertionError("write_pots must not run when the employee is missing")


def _use_missing(monkeypatch) -> None:
    """Install a StaffBalances that cannot find the employee."""
    async def _get():
        return _NoStaffBalances()
    monkeypatch.setattr(rd, "get_staff_balances", _get)


def test_approve_of_an_unmatched_employee_is_a_clean_error(monkeypatch):
    _use_missing(monkeypatch)
    record = _leave(leave_type="Sick or Personal Day", days=2)

    result = run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))

    assert result == {"error": "Employee not found"}
    fresh, ledger, events, outbox = run(_inspect(record))
    assert fresh.status == "pending"                              # nothing decided, nothing moved
    assert ledger == [] and outbox == []


def test_refund_of_an_unmatched_employee_is_a_clean_error(monkeypatch):
    # Approve normally so the refund gets past its status gate, then make the
    # staff row unlockable at refund time.
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)
    run(rd.approve("leave", record.item_id, "Boss", "manager_dashboard"))
    _use_missing(monkeypatch)                                     # the staff row is gone now

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result == {"error": "Employee not found"}
    fresh, _ledger, _events, _outbox = run(_inspect(record))
    assert fresh.status == "approved"                             # still approved; the refund did nothing


# ----- the result names who actually decided (finding 10) -----


def test_an_admin_decision_is_named_as_an_administrator(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2)

    run(rd.approve("leave", record.item_id, "admin", "admin_dashboard"))

    _fresh, _ledger, _events, outbox = run(_inspect(record))
    assert outbox and "by an administrator" in outbox[0].body    # not the assigned manager's name


def test_a_manager_decision_is_named_by_the_manager(monkeypatch):
    _use(monkeypatch, _all_pots(sick=5))
    record = _leave(leave_type="Sick or Personal Day", days=2, manager_name="Boss Person")

    run(rd.approve("leave", record.item_id, "Boss Person", "manager_dashboard"))

    _fresh, _ledger, _events, outbox = run(_inspect(record))
    assert outbox and "by Boss Person" in outbox[0].body
