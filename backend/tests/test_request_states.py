"""The request state machine and its wiring (#455).

Covers the pure state-machine module (the moves table, can_decide, the backfill
mapping), the create/submit/decision write paths that move the state, the
"still getting ready" refusal on the decision path (every channel funnels into
``request_decisions.approve``/``reject``), the stuck rule, the timeline actor
name resolution, and own-request approval now being allowed.

Like the other decision tests these run against the SQLite test database with an
in-memory StaffBalances, so nothing touches a real backend.
"""
import asyncio
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.config import settings
from app.models.request_record import RequestRecord
from app.models.request_event import RequestEvent
from app.models.staff import Staff
from app.repositories.postgres.typed_requests import TypedRequestList
from app.repositories.staff_balances import POT_COLUMNS
from app.services import request_decisions as rd
from app.services import request_submission as rs
from app.services import request_states as st
from app.routes import dashboard as dash
from app.routes import events as ev

# Unique ids per insert, so the per-employee lock and the counters never carry
# between tests sharing the one SQLite database.
_item_ids = itertools.count(70_000_000)
_employee_ids = itertools.count(700_000)
LEAVE = settings.SP_LIST_LEAVE_REQUESTS


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_requests():
    """Clear the request tables before each test, so a rerun never clashes on item_id."""
    from app.models.balance_ledger import BalanceLedger
    from app.models.outbox import Outbox
    from app.models.request_change import RequestChange

    async def wipe():
        async with async_session() as session:
            for model in (Outbox, BalanceLedger, RequestChange, RequestEvent, RequestRecord):
                await session.execute(delete(model))               # children first, then the request rows
            await session.commit()

    run(wipe())
    yield


# ----- the pure state-machine module -----

def test_can_decide_is_true_only_for_waiting_on_approval():
    """Only a waiting_on_approval request may be decided."""
    class R:                                                       # a stand-in with just a state
        pass
    for state in st.STATES:
        r = R()
        r.state = state
        assert st.can_decide(r) is (state == "waiting_on_approval")


def test_the_moves_table_allows_the_agreed_transitions():
    """Every legal move in the agreed table is accepted and applied."""
    legal = [
        ("getting_ready", "waiting_on_approval"),
        ("getting_ready", "rejected"),
        ("getting_ready", "approved"),
        ("getting_ready", "needs_attention"),
        ("needs_attention", "waiting_on_approval"),
        ("needs_attention", "getting_ready"),
        ("waiting_on_approval", "approved"),
        ("waiting_on_approval", "rejected"),
        ("waiting_on_approval", "cancelled"),
        ("waiting_on_approval", "waiting_on_approval"),
        ("approved", "refunded"),
        ("approved", "cancelled"),
    ]
    for frm, to in legal:
        rec = RequestRecord(kind="leave", item_id=1, employee_id="1", state=frm)
        assert st.move(rec, to) == to and rec.state == to


def test_the_moves_table_refuses_illegal_transitions():
    """An illegal move raises and leaves the state untouched."""
    illegal = [
        ("getting_ready", "refunded"),        # must be approved first
        ("getting_ready", "cancelled"),       # not a direct move
        ("waiting_on_approval", "getting_ready"),
        ("approved", "approved"),             # already decided
        ("approved", "rejected"),             # cannot reject an approved request
        ("rejected", "waiting_on_approval"),  # rejected is final
        ("refunded", "approved"),             # refunded is final
        ("cancelled", "waiting_on_approval"),  # cancelled is final
    ]
    for frm, to in illegal:
        rec = RequestRecord(kind="leave", item_id=1, employee_id="1", state=frm)
        with pytest.raises(st.IllegalStateMove):
            st.move(rec, to)
        assert rec.state == frm                                    # unchanged


def test_move_refuses_an_unknown_target():
    """A typo'd target state is refused rather than written."""
    rec = RequestRecord(kind="leave", item_id=1, employee_id="1", state="getting_ready")
    with pytest.raises(st.IllegalStateMove):
        st.move(rec, "not_a_state")


def test_backfill_maps_decided_statuses_straight_across():
    """A decided status always maps to the matching state."""
    for status in ("approved", "rejected", "refunded", "cancelled"):
        assert st.backfill_state(status, has_manager=False, has_submit_event=False, legacy=False) == status


def test_backfill_pending_with_a_manager_and_an_event_is_waiting():
    """A pending row ready to be decided backfills to waiting_on_approval."""
    assert st.backfill_state("pending", has_manager=True, has_submit_event=True, legacy=False) == "waiting_on_approval"


def test_backfill_a_legacy_pending_row_is_waiting_even_without_a_manager():
    """A legacy pending row is treated as ready, no manager needed."""
    assert st.backfill_state("pending", has_manager=False, has_submit_event=False, legacy=True) == "waiting_on_approval"


def test_backfill_an_unfinished_pending_row_needs_attention():
    """A pending row the old pipeline never finished backfills to needs_attention."""
    assert st.backfill_state("pending", has_manager=False, has_submit_event=True, legacy=False) == "needs_attention"
    assert st.backfill_state("pending", has_manager=True, has_submit_event=False, legacy=False) == "needs_attention"


# ----- create and the decision write paths -----

def test_a_fresh_create_starts_getting_ready():
    """A new pending request is created in the getting_ready state."""
    item = run(TypedRequestList("leave").create_list_item(
        LEAVE, {"LeaveType": "Vacation", "StartDate": "2026-11-02", "Days": 2, "EmployeeID": "5"},
    ))
    assert item["fields"]["state"] == "getting_ready"
    assert item["fields"]["Status"] == "Pending"


def test_mark_decided_moves_the_state_for_each_outcome():
    """_mark_decided moves the state to match approve / reject / refund."""
    approved = RequestRecord(kind="leave", item_id=1, employee_id="1", state="waiting_on_approval")
    rd._mark_decided(approved, "approved", "9", "manager_dashboard")
    assert approved.state == "approved"

    rejected = RequestRecord(kind="leave", item_id=2, employee_id="1", state="waiting_on_approval")
    rd._mark_decided(rejected, "rejected", "9", "manager_dashboard")
    assert rejected.state == "rejected"

    refunded = RequestRecord(kind="leave", item_id=3, employee_id="1", state="approved")
    rd._mark_decided(refunded, "refunded", "admin", "admin_dashboard")
    assert refunded.state == "refunded"


def test_auto_reject_at_submit_moves_getting_ready_to_rejected():
    """A submit-time auto-rejection moves the state to rejected."""
    rec = RequestRecord(kind="leave", item_id=1, employee_id="1", state="getting_ready")
    rs._mark_auto_rejected(rec)
    assert rec.status == "rejected" and rec.state == "rejected"


# ----- the "still getting ready" refusal on the decision path -----

async def _seed(state="waiting_on_approval", **fields):
    """Insert one leave request in a given state and return its item_id."""
    base = dict(
        kind="leave", item_id=next(_item_ids), status="pending", state=state,
        employee_id=str(next(_employee_ids)), employee_name="Pat Staff",
        employee_email="pat@ucsh.com", manager_name="Boss", employment_type="Salary",
        leave_type="Sick or Personal Day", start_date=date(2026, 11, 2), days=1.0,
        approval_version=1, balance_state="not_applied",
    )
    base.update(fields)
    async with async_session() as session:
        record = RequestRecord(**base)
        session.add(record)
        await session.commit()
        return record.item_id


@pytest.mark.parametrize("channel", ["manager_dashboard", "admin_dashboard", "email_link", "sms"])
def test_a_getting_ready_request_cannot_be_approved_on_any_channel(channel):
    """approve refuses a still-getting-ready request, whatever channel asks."""
    item_id = run(_seed(state="getting_ready"))
    result = run(rd.approve("leave", item_id, "9", channel))
    assert result.get("refused") == "not_ready"
    assert result["error"] == rd.NOT_READY_MESSAGE


@pytest.mark.parametrize("channel", ["manager_dashboard", "admin_dashboard", "email_link", "sms"])
def test_a_getting_ready_request_cannot_be_rejected_on_any_channel(channel):
    """reject refuses a still-getting-ready request, whatever channel asks."""
    item_id = run(_seed(state="getting_ready"))
    result = run(rd.reject("leave", item_id, "9", channel, reason="no"))
    assert result.get("refused") == "not_ready"
    assert result["error"] == rd.NOT_READY_MESSAGE


def test_a_needs_attention_request_cannot_be_decided():
    """A needs_attention request is refused as not ready too."""
    item_id = run(_seed(state="needs_attention"))
    assert run(rd.approve("leave", item_id, "9", "manager_dashboard")).get("refused") == "not_ready"


def test_a_getting_ready_refusal_records_a_refused_event():
    """The refusal leaves a refused event with the not_ready reason."""
    item_id = run(_seed(state="getting_ready"))
    run(rd.approve("leave", item_id, "9", "manager_dashboard"))

    async def read():
        async with async_session() as session:
            rec = (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.item_id == item_id)
            )).scalars().first()
            return (await session.execute(
                select(RequestEvent).where(RequestEvent.request_id == rec.id, RequestEvent.type == "refused")
            )).scalars().all()

    refused = run(read())
    assert refused and refused[0].detail.get("reason") == rd.NOT_READY_MESSAGE


# ----- approve / reject move a waiting request -----

def _use_balances(monkeypatch, **pots):
    """Install an in-memory StaffBalances for the decision path."""
    base = {pot: 0.0 for pot in POT_COLUMNS}
    base.update(pots)
    store = dict(base)

    class Fake:
        async def lock_pots(self, session, employee_id):
            return dict(store)

        async def write_pots(self, session, employee_id, new_values):
            store.update(new_values)

    async def _get():
        return Fake()

    monkeypatch.setattr(rd, "get_staff_balances", _get)


def test_approving_a_waiting_request_moves_it_to_approved(monkeypatch):
    """A real approve of a waiting request moves the state to approved."""
    _use_balances(monkeypatch, sick=5.0)
    item_id = run(_seed(state="waiting_on_approval"))
    result = run(rd.approve("leave", item_id, "9", "manager_dashboard"))
    assert result.get("status") == "approved"

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.item_id == item_id)
            )).scalars().first()

    rec = run(read())
    assert rec.status == "approved" and rec.state == "approved"


def test_rejecting_a_waiting_request_moves_it_to_rejected(monkeypatch):
    """A real reject of a waiting request moves the state to rejected."""
    item_id = run(_seed(state="waiting_on_approval"))
    result = run(rd.reject("leave", item_id, "9", "manager_dashboard", reason="too busy"))
    assert result.get("status") == "rejected"

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.item_id == item_id)
            )).scalars().first()

    assert run(read()).state == "rejected"


# ----- an admin edit keeps a request decidable -----

def test_an_admin_edit_keeps_the_request_waiting_on_approval(monkeypatch):
    """Editing a pending request leaves it waiting_on_approval under a new version."""
    async def _no_employee(record, fields):
        return None                                                # no staff lookup in this test

    monkeypatch.setattr(rs, "_resolve_employee", _no_employee)     # avoid a real Graph call
    item_id = run(_seed(state="waiting_on_approval"))
    result = run(rs.apply_admin_edit(
        "leave", item_id, before={"Days": 1}, after={"Days": 2}, reason="fix the days",
    ))
    assert result.get("status") == "saved" and result["new_version"] == 2

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.item_id == item_id)
            )).scalars().first()

    rec = run(read())
    assert rec.state == "waiting_on_approval" and rec.approval_version == 2


# ----- reprocessing a needs_attention request -----

def test_reprocessing_a_needs_attention_request_makes_it_waiting(monkeypatch):
    """A successful reprocess moves needs_attention back to waiting_on_approval."""
    item_id = run(_seed(state="needs_attention"))
    employee = {"id": "1", "fields": {"Title": "Pat Staff", "EmailAddress": "pat@ucsh.com"}}
    manager = {"id": "9", "fields": {"Title": "Boss", "EmailAddress": "boss@ucsh.com"}}

    async def _emp(record, fields):
        return employee

    async def _mgrs(emp):
        return [manager]

    async def _choose(session, managers, today):
        return manager, []

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(rs, "_resolve_employee", _emp)
    monkeypatch.setattr(rs, "get_all_managers_for_employee", _mgrs)
    monkeypatch.setattr(rs, "choose_available_manager", _choose)
    monkeypatch.setattr(rs, "_enqueue_notifications", _noop)
    monkeypatch.setattr(rs, "_wake_sender", lambda: None)

    assert run(rs.reprocess_request("leave", item_id, "please look again")) is True

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.item_id == item_id)
            )).scalars().first()

    assert run(read()).state == "waiting_on_approval"


# ----- the stuck rule -----

def test_getting_ready_is_stuck_only_after_the_window():
    """A getting_ready row is stuck once it is older than the window."""
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    just_now = datetime.now(timezone.utc).isoformat()
    assert dash._getting_ready_stuck(long_ago) is True
    assert dash._getting_ready_stuck(just_now) is False
    assert dash._getting_ready_stuck(None) is False


# ----- the timeline names who acted -----

def test_the_timeline_resolves_a_numeric_actor_to_a_staff_name():
    """A numeric actor id is resolved to the staff name; an unknown id stays raw."""
    async def seed_staff():
        async with async_session() as session:
            await session.execute(delete(Staff).where(Staff.sp_item_id == "416"))
            session.add(Staff(sp_item_id="416", name="Dave Approver", email="dave@ucsh.com"))
            await session.commit()

    run(seed_staff())

    async def names():
        async with async_session() as session:
            return await ev._actor_names_for(session, {"416", "999", "admin"})

    resolved = run(names())
    assert resolved == {"416": "Dave Approver"}                    # only the known numeric id resolves

    class E:                                                       # a stand-in event
        type = "approved"
        channel = "manager_dashboard"
        actor = "416"
        detail = {}

    assert "by Dave Approver" in ev._timeline_line(E(), resolved)  # the name, not the id
    E.actor = "999"
    assert "by 999" in ev._timeline_line(E(), resolved)            # unknown id falls back to the raw value


# ----- approving your own request is allowed -----

def test_a_manager_may_decide_their_own_request_on_the_dashboard(monkeypatch):
    """The own-team gate permits a manager deciding their own request (#455)."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    item_id = run(_seed(state="waiting_on_approval", employee_id="321", manager_id="321"))

    class User:
        role = "manager"
        user_id = "321"

    # Does not raise: the caller's own request is allowed.
    run(dash._refuse_manager_not_on_request(User(), "leave", str(item_id)))
