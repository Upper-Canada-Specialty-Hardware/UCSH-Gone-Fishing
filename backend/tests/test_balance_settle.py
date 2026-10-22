"""Settling approved requests that never moved a balance (#399, #400, #401).

Each test inserts an approved request whose balance_state is still
``not_applied`` (what the setup check flags), settles it one of the three ways,
and checks the pots, the ledger rows, the new balance_state, the timeline event
and the reason together, since they land in one transaction. An in-memory
StaffBalances stands in for the staff table, as in the decision tests.
"""
import asyncio
import itertools
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.legacy_finding import LegacyFinding
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.repositories.staff_balances import POT_COLUMNS
from app.services import balance_settle as bs
from app.services import request_decisions as rd

_item_ids = itertools.count(70_000_000)                          # unique per insert, clear of other tests
_employee_ids = itertools.count(950_000)


def run(coro):
    """Run one coroutine in its own event loop."""
    return asyncio.run(coro)


class FakeStaffBalances:
    """An in-memory StaffBalances that remembers every write."""

    def __init__(self, pots: dict[str, float]):
        """Start with these pots.

        Args:
            pots: ``{pot: value}`` for every stored pot.
        """
        self._pots = dict(pots)
        self.write_calls: list[dict] = []

    async def lock_pots(self, session, employee_id):
        """A copy of the pots now."""
        return dict(self._pots)

    async def write_pots(self, session, employee_id, new_values):
        """Apply and remember one write."""
        self.write_calls.append(dict(new_values))
        self._pots.update(new_values)


def _pots(**overrides) -> dict[str, float]:
    """Every pot at zero, with the named ones set."""
    pots = {pot: 0.0 for pot in POT_COLUMNS}
    pots.update(overrides)
    return pots


def _use(monkeypatch, pots: dict[str, float]) -> FakeStaffBalances:
    """Point the settle path at a fake balance store, with the switch on.

    Args:
        monkeypatch: The pytest fixture.
        pots: Starting pots.

    Returns:
        The fake, to read its writes.
    """
    fake = FakeStaffBalances(pots)

    async def _get():
        return fake

    async def _on():
        return True

    async def _no_followups(*args, **kwargs):
        return None

    monkeypatch.setattr(bs, "get_staff_balances", _get)
    monkeypatch.setattr(rd, "get_staff_balances", _get)           # the refund test goes through the decision path
    monkeypatch.setattr(bs, "decisions_active", _on)
    monkeypatch.setattr(bs, "_after_decision_followups", _no_followups)
    monkeypatch.setattr(rd, "_after_decision_followups", _no_followups)
    return fake


def _approved(**fields) -> RequestRecord:
    """Insert an approved, never-applied leave and return it."""
    base = dict(
        kind="leave", item_id=next(_item_ids), status="approved", stage="Result sent",
        employee_id=str(next(_employee_ids)), employee_name="Dana Whitfield",
        employee_email="dana@ucsh.com", manager_name="Boss Person", employment_type="Salary",
        approval_version=1, balance_state="not_applied", leave_type="Vacation",
        start_date=date(2026, 8, 17), end_date=date(2026, 8, 18), days=2,
        decided_at=datetime(2026, 7, 27, tzinfo=timezone.utc), extra={},
    )
    base.update(fields)

    async def insert():
        async with async_session() as session:
            record = RequestRecord(**base)
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    return run(insert())


async def _inspect(record: RequestRecord):
    """Re-read the request and its ledger, events, changes and outbox rows."""
    async with async_session() as session:
        fresh = await session.get(RequestRecord, record.id)

        async def rows(model):
            return (await session.execute(select(model).where(model.request_id == record.id))).scalars().all()

        return fresh, await rows(BalanceLedger), await rows(RequestEvent), await rows(RequestChange), await rows(Outbox)


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the tables these tests write, before and after each."""
    async def wipe():
        async with async_session() as session:
            for model in (BalanceLedger, RequestEvent, Outbox, RequestChange, LegacyFinding, RequestRecord):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


# ----- take it off now -----


def test_take_it_off_now_moves_the_pots_like_an_approval(monkeypatch):
    fake = _use(monkeypatch, _pots(make_up=0.5, vacation=8))
    record = _approved(days=2)

    result = run(bs.settle("leave", record.item_id, "now", "approved in July, never deducted"))

    assert result["outcome"] == "now" and result["balance_state"] == "applied"
    # Vacation leave spends Make-Up first: 0.5 from Make-Up, the other 1.5 from vacation.
    assert fake.write_calls == [{"make_up": 0.0, "vacation": 6.5}]
    fresh, ledger, events, changes, outbox = run(_inspect(record))
    assert fresh.balance_state == "applied" and fresh.status == "approved"
    assert sorted((row.pot, float(row.change)) for row in ledger) == [("make_up", -0.5), ("vacation", -1.5)]
    assert all("taken off now" in row.reason for row in ledger)
    event = next(e for e in events if e.type == "balance_applied")
    assert event.detail == {"settled": "now", "reason": "approved in July, never deducted"}
    assert [(c.field, c.old_value, c.new_value, c.reason) for c in changes] == [
        ("balance_state", "not_applied", "applied", "approved in July, never deducted")]
    assert outbox == []                                         # bookkeeping: nobody is emailed


def test_a_carry_over_past_zero_cannot_be_taken_off_now(monkeypatch):
    fake = _use(monkeypatch, _pots(vacation=1))
    record = _approved(kind="carryover_payout", leave_type=None, request_option="payout", days=3,
                       start_date=None, end_date=None)

    with pytest.raises(bs.SettleError) as err:
        run(bs.settle("carryover_payout", record.item_id, "now", "reason"))

    assert err.value.status_code == 409
    assert fake.write_calls == []
    assert run(_inspect(record))[0].balance_state == "not_applied"   # nothing written


# ----- already taken off by hand -----


def test_already_taken_off_by_hand_records_the_pots_without_moving_them(monkeypatch):
    fake = _use(monkeypatch, _pots(vacation=6))
    record = _approved(days=2)

    result = run(bs.settle("leave", record.item_id, "hand", "HR lowered it by hand", {"vacation": 2}))

    assert result["balance_state"] == "applied"
    assert fake.write_calls == []                               # the balance already reflects it
    fresh, ledger, events, changes, outbox = run(_inspect(record))
    assert len(ledger) == 1
    row = ledger[0]
    # Recorded as the hand edit was: 8 -> 6, so "Taken from" reads vacation 2.
    assert row.pot == "vacation" and float(row.before) == 8.0 and float(row.after) == 6.0
    assert "already taken off by hand" in row.reason


def test_hand_amounts_must_add_up_to_the_request(monkeypatch):
    _use(monkeypatch, _pots(vacation=6))
    record = _approved(days=2)

    with pytest.raises(bs.SettleError) as err:
        run(bs.settle("leave", record.item_id, "hand", "reason", {"vacation": 1.5}))

    assert "1.5" in str(err.value) and err.value.status_code == 400


def test_hand_amounts_cannot_name_payout_or_go_negative(monkeypatch):
    _use(monkeypatch, _pots(vacation=6))
    record = _approved(days=2)

    with pytest.raises(bs.SettleError):
        run(bs.settle("leave", record.item_id, "hand", "reason", {"payout": 2}))
    with pytest.raises(bs.SettleError):
        run(bs.settle("leave", record.item_id, "hand", "reason", {"vacation": 3, "sick": -1}))


def test_a_hand_settled_request_refunds_what_was_recorded(monkeypatch):
    fake = _use(monkeypatch, _pots(vacation=6))
    record = _approved(days=2)
    run(bs.settle("leave", record.item_id, "hand", "HR lowered it by hand", {"vacation": 2}))

    result = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))

    assert result["status"] == "refunded"
    assert fake.write_calls == [{"vacation": 8.0}]              # the two recorded days come back


# ----- no balance needed -----


def test_no_balance_needed_moves_nothing_and_a_refund_gives_nothing_back(monkeypatch):
    fake = _use(monkeypatch, _pots(vacation=8))
    record = _approved(days=1)

    result = run(bs.settle("leave", record.item_id, "none", "test request"))

    assert result["balance_state"] == "no_balance"
    fresh, ledger, events, changes, outbox = run(_inspect(record))
    assert ledger == [] and fake.write_calls == []
    assert any(e.type == "no_balance_used" and e.detail["settled"] == "none" for e in events)
    # A later refund must not re-derive days that were never taken.
    refund = run(rd.refund("leave", record.item_id, "admin", "admin_dashboard"))
    assert refund["no_balance_change"] is True and fake.write_calls == []


# ----- refusals and the finding -----


def test_a_reason_is_required(monkeypatch):
    _use(monkeypatch, _pots(vacation=8))
    record = _approved()
    with pytest.raises(bs.SettleError):
        run(bs.settle("leave", record.item_id, "none", "   "))


def test_a_settled_request_cannot_be_settled_again(monkeypatch):
    _use(monkeypatch, _pots(vacation=8))
    record = _approved()
    run(bs.settle("leave", record.item_id, "none", "test request"))
    with pytest.raises(bs.SettleError) as err:
        run(bs.settle("leave", record.item_id, "now", "again"))
    assert err.value.status_code == 409


def test_a_pending_request_cannot_be_settled(monkeypatch):
    _use(monkeypatch, _pots(vacation=8))
    record = _approved(status="pending", decided_at=None)
    with pytest.raises(bs.SettleError) as err:
        run(bs.settle("leave", record.item_id, "now", "reason"))
    assert err.value.status_code == 409


def test_settling_resolves_the_importers_open_finding(monkeypatch):
    _use(monkeypatch, _pots(vacation=8))
    record = _approved()

    async def add_finding():
        async with async_session() as session:
            session.add(LegacyFinding(
                case="approved_no_balance_change", subject="request", kind="leave",
                subject_id=str(record.item_id), reason="approved, no balance change",
                evidence={}, fingerprint=f"approved_no_balance_change:request:leave:{record.item_id}",
            ))
            await session.commit()

    run(add_finding())
    run(bs.settle("leave", record.item_id, "none", "test request"))

    async def read():
        async with async_session() as session:
            return (await session.execute(select(LegacyFinding))).scalars().all()

    finding = run(read())[0]
    assert finding.status == "resolved" and finding.note == "test request"


def test_preview_shows_the_planned_moves_and_writes_nothing(monkeypatch):
    fake = _use(monkeypatch, _pots(make_up=0.5, vacation=8))
    record = _approved(days=2)

    preview = run(bs.settle_preview("leave", record.item_id))

    assert preview["plan"]["outcome"] == "balance_applied" and preview["hand_editable"] is True
    assert [(m["pot"], m["change"]) for m in preview["plan"]["moves"]] == [("make_up", -0.5), ("vacation", -1.5)]
    assert preview["request"]["days"] == 2.0
    assert fake.write_calls == []
    assert run(_inspect(record))[0].balance_state == "not_applied"
