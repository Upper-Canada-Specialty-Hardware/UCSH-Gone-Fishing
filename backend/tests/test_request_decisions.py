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
