"""Staff change or cancel their own pending request (#518, #519).

Covers the service end to end on the test database: the cancel (status, the
change row, the timeline event, the manager's and the person's notices in the
request's one thread, an old approval link refused), the change (the edited
row, the new version, the manager's re-ask and the person's receipt each opening
with what changed), the signed cancel link in the receipt, and the submit-time
split between an approved clash (refused) and a pending one (the offer).
"""
import asyncio
import itertools
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models import RequestApprovalState
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services import own_requests
from app.services import request_decisions as rd
from app.services import request_intake
from app.services import request_submission as rsub
from app.services.cancel_links import cancel_token, decode_cancel_token
from app.services.own_requests import (
    OwnRequestError,
    cancel_from_link,
    cancel_own_request,
    cancel_page_info,
    change_own_leave,
)
from app.services.request_intake import LeaveForm, PendingClash, RequestFormError
from app.services.request_threads import thread_subject

_item_ids = itertools.count(96_000_000)

_EMPLOYEE = {"id": "7", "fields": {"Title": "Casey Demo", "EmailAddress": "casey@ucsh.com",
                                   "SalaryHourly": "Hourly", "Location": ""}}
_MANAGER = {"id": "5", "fields": {"Title": "Morgan Boss", "EmailAddress": "mgr@ucsh.com",
                                  "CellNumber": "+15555550101"}}


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the request tables before and after each test."""
    async def wipe():
        async with async_session() as session:
            for model in (Outbox, RequestEvent, RequestChange, RequestRecord,
                          RequestCounter, RequestApprovalState):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


@pytest.fixture(autouse=True)
def _routing(monkeypatch):
    """One employee, one available manager, no holidays, and no real sender wake-up."""
    async def resolve(record, fields):
        return _EMPLOYEE

    async def managers(employee):
        return [_MANAGER]

    async def choose(session, mgrs, on_date):
        return _MANAGER, []

    async def by_id(staff_id):
        return _MANAGER if str(staff_id) == "5" else None

    async def no_holidays(province):
        return []

    monkeypatch.setattr(rsub, "_resolve_employee", resolve)
    monkeypatch.setattr(rsub, "get_all_managers_for_employee", managers)
    monkeypatch.setattr(rsub, "choose_available_manager", choose)
    monkeypatch.setattr(own_requests, "get_employee_by_id", by_id)
    monkeypatch.setattr(own_requests, "get_holidays_for_province", no_holidays)
    monkeypatch.setattr(own_requests, "_wake_sender", lambda: None)
    monkeypatch.setattr(rsub, "_wake_sender", lambda: None)


def _no_clashes(monkeypatch, clashes=None):
    """Stub the leave-list read the clash check makes."""
    async def check(**kwargs):
        return list(clashes or [])
    monkeypatch.setattr(request_intake, "check_new_leave_clashes", check)


async def _leave(**overrides) -> RequestRecord:
    """Insert one pending leave row for Casey, asked of Morgan."""
    values = dict(
        kind="leave", item_id=next(_item_ids), status="pending",
        employee_id="7", employee_email="casey@ucsh.com", employee_name="Casey Demo",
        leave_type="Vacation", start_date=date(2026, 11, 2), end_date=date(2026, 11, 6), days=5,
        manager_id="5", manager_name="Morgan Boss", approval_version=1, balance_state="not_applied",
        state="waiting_on_approval",                          # sent to the manager (#455)
    )
    values.update(overrides)
    async with async_session() as session:
        record = RequestRecord(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def _read(record_id):
    """The row, its outbox messages, its events and its change rows."""
    async with async_session() as session:
        row = await session.get(RequestRecord, record_id)
        outbox = (await session.execute(select(Outbox).where(Outbox.request_id == record_id))).scalars().all()
        events = (await session.execute(select(RequestEvent).where(RequestEvent.request_id == record_id))).scalars().all()
        changes = (await session.execute(select(RequestChange).where(RequestChange.request_id == record_id))).scalars().all()
        return row, outbox, events, changes


# ===== cancel (#519) =====


def test_cancel_marks_it_cancelled_and_tells_the_manager_and_the_person_in_its_thread():
    record = run(_leave())
    result = run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    assert result == {"status": "cancelled", "item_id": str(record.item_id), "already": False}

    row, outbox, events, changes = run(_read(record.id))
    assert row.status == "cancelled"
    assert any(e.type == "cancelled" for e in events)
    assert any(c.field == "Status" and c.new_value == "Cancelled" for c in changes)
    emails = {r.recipient: r for r in outbox if r.channel == "email"}
    assert set(emails) == {"mgr@ucsh.com", "casey@ucsh.com"}
    assert all(r.subject == thread_subject(row) for r in emails.values())    # the request's one thread (#520)
    assert "Casey Demo cancelled this request" in emails["mgr@ucsh.com"].body
    assert "You cancelled this request" in emails["casey@ucsh.com"].body
    assert [r.recipient for r in outbox if r.channel == "sms"] == ["+15555550101"]   # the ask went by text too
    assert all(r.message_type == "cancel_notice" for r in outbox)


def test_an_old_approval_link_on_a_cancelled_request_says_it_was_cancelled():
    record = run(_leave())
    run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    approved = run(rd.approve("leave", record.item_id, "Morgan Boss", "email_link", link_version=1))
    rejected = run(rd.reject("leave", record.item_id, "Morgan Boss", "sms"))
    assert approved["error"] == rd.CANCELLED_MESSAGE
    assert rejected["error"] == rd.CANCELLED_MESSAGE


def test_cancelling_twice_changes_nothing_the_second_time():
    record = run(_leave())
    run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    again = run(cancel_own_request("leave", record.item_id, "7", channel="employee_dashboard"))
    assert again["already"] is True
    _, outbox, _, _ = run(_read(record.id))
    assert len(outbox) == 3                                   # still only the first cancel's notices


def test_someone_elses_request_reads_as_gone():
    record = run(_leave())
    with pytest.raises(OwnRequestError) as exc:
        run(cancel_own_request("leave", record.item_id, "8", channel="request_page"))
    assert exc.value.status_code == 404


def test_an_approved_request_is_refunded_by_an_admin_not_cancelled():
    record = run(_leave(status="approved"))
    with pytest.raises(OwnRequestError) as exc:
        run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    assert exc.value.status_code == 409 and "ask an admin for a refund" in str(exc.value)


def test_a_request_still_being_set_up_cannot_be_cancelled_yet():
    # It has not reached a manager, and #455 allows no move to cancelled from there.
    record = run(_leave(state="getting_ready"))
    with pytest.raises(OwnRequestError) as exc:
        run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    assert exc.value.status_code == 409 and "still being set up" in str(exc.value)
    row, outbox, _, _ = run(_read(record.id))
    assert row.status == "pending" and outbox == []          # nothing changed, nobody told


def test_cancel_moves_the_state_to_cancelled():
    record = run(_leave())
    run(cancel_own_request("leave", record.item_id, "7", channel="request_page"))
    row, _, _, _ = run(_read(record.id))
    assert row.state == "cancelled"                           # the lifecycle state follows (#455)


def test_overtime_can_be_cancelled_too():
    record = run(_leave(kind="overtime", leave_type=None, end_date=None, days=None, hours=3))
    run(cancel_own_request("overtime", record.item_id, "7", channel="request_page"))
    row, _, _, _ = run(_read(record.id))
    assert row.status == "cancelled"


# ===== the cancel link in the receipt (#519) =====


def test_the_cancel_token_round_trips_and_a_forged_one_does_not():
    token = cancel_token("leave", 12, "7")
    assert decode_cancel_token(token) == ("leave", 12, "7")
    assert decode_cancel_token(token.replace(".7.", ".8.")) is None   # someone else's staff id
    assert decode_cancel_token("nonsense") is None


def test_the_cancel_link_page_shows_the_request_then_cancels_it_once():
    record = run(_leave())
    token = cancel_token("leave", record.item_id, "7")
    info = run(cancel_page_info(token))
    assert info["state"] == "open" and info["request_number"] == str(record.item_id)
    assert info["summary"] == "Vacation, Nov 2, 2026 to Nov 6, 2026 (5 days)"
    assert run(cancel_from_link(token))["state"] == "done"
    assert run(cancel_from_link(token))["state"] == "cancelled"     # a second click: says what it is now


def test_the_receipt_carries_the_persons_own_cancel_link():
    record = run(_leave())

    async def queue():
        async with async_session() as session:
            fresh = await session.get(RequestRecord, record.id)
            await rsub._enqueue_notifications(session, fresh, _EMPLOYEE, [_MANAGER], event=None)
            await session.commit()

    run(queue())
    _, outbox, _, _ = run(_read(record.id))
    receipt = next(r for r in outbox if r.message_type == "receipt")
    assert "#/cancel?token=" + cancel_token("leave", record.item_id, "7") in receipt.body
    ask = next(r for r in outbox if r.message_type == "manager_approval" and r.channel == "email")
    assert "#/cancel?token=" not in ask.body                  # the manager never gets the person's link


# ===== change (#518) =====


def _form(**values) -> LeaveForm:
    """A checked leave form."""
    data = {"leave_type": "Vacation", "start_date": "2026-11-03", "end_date": "2026-11-09"}
    data.update(values)
    return LeaveForm.model_validate(data)


def test_a_change_keeps_the_request_and_tells_the_manager_what_changed(monkeypatch):
    _no_clashes(monkeypatch)
    record = run(_leave())
    result = run(change_own_leave(record.item_id, _EMPLOYEE, _form()))
    assert result == {"status": "changed", "item_id": str(record.item_id)}

    row, outbox, events, changes = run(_read(record.id))
    assert (row.start_date, row.end_date, float(row.days)) == (date(2026, 11, 3), date(2026, 11, 9), 5.0)
    assert row.approval_version == 2                          # old links go stale
    assert any(e.type == "edited" for e in events)
    assert {c.field for c in changes} >= {"StartDate", "EndDate"}
    ask = next(r for r in outbox if r.message_type == "manager_approval" and r.channel == "email")
    assert "Casey Demo changed this request" in ask.body
    assert "It was: Vacation, Nov 2, 2026 to Nov 6, 2026 (5 days)" in ask.body
    assert "It is now: Vacation, Nov 3, 2026 to Nov 9, 2026 (5 days)" in ask.body
    receipt = next(r for r in outbox if r.message_type == "receipt")
    assert "You changed this request" in receipt.body
    assert ask.subject == receipt.subject == thread_subject(row)   # same thread as before (#520)


def test_a_change_that_changes_nothing_is_refused(monkeypatch):
    _no_clashes(monkeypatch)
    record = run(_leave())
    with pytest.raises(OwnRequestError) as exc:
        run(change_own_leave(record.item_id, _EMPLOYEE, _form(start_date="2026-11-02", end_date="2026-11-06",
                                                             notes=None)))
    # The stored title is built from the name and the notes, as at submit.
    assert exc.value.status_code == 400 and "nothing was changed" in str(exc.value)


def test_a_change_onto_another_pending_request_is_refused(monkeypatch):
    _no_clashes(monkeypatch, [{"item_id": "55", "start_date": "2026-11-09", "end_date": "2026-11-09",
                               "status": "Pending", "leave_type": "Vacation", "days": 1}])
    record = run(_leave())
    with pytest.raises(OwnRequestError) as exc:
        run(change_own_leave(record.item_id, _EMPLOYEE, _form()))
    assert exc.value.status_code == 409 and "#55" in str(exc.value)


def test_a_change_onto_approved_leave_is_refused(monkeypatch):
    _no_clashes(monkeypatch, [{"item_id": "56", "start_date": "2026-11-09", "end_date": "2026-11-09",
                               "status": "Approved", "leave_type": "Vacation", "days": 1}])
    record = run(_leave())
    with pytest.raises(OwnRequestError) as exc:
        run(change_own_leave(record.item_id, _EMPLOYEE, _form()))
    assert exc.value.status_code == 409 and "approved time off" in str(exc.value)


def test_a_decided_request_cannot_be_changed(monkeypatch):
    _no_clashes(monkeypatch)
    record = run(_leave(status="rejected"))
    with pytest.raises(OwnRequestError) as exc:
        run(change_own_leave(record.item_id, _EMPLOYEE, _form()))
    assert exc.value.status_code == 409


# ===== submit: approved refused, pending offered (#446, #518) =====


def test_a_pending_clash_at_submit_raises_the_offer_and_an_approved_one_refuses(monkeypatch):
    pending = {"item_id": "60", "start_date": "2026-11-02", "end_date": "2026-11-06",
               "status": "Pending", "leave_type": "Vacation", "days": 5}
    _no_clashes(monkeypatch, [pending])
    with pytest.raises(PendingClash) as exc:
        run(request_intake.refuse_leave_clashes("2026-11-03", "2026-11-03", "casey@ucsh.com", "7"))
    assert exc.value.clashes == [{"item_id": "60", "when": "Nov 2, 2026 to Nov 6, 2026",
                                  "leave_type": "Vacation", "days": 5}]

    approved = {**pending, "item_id": "61", "status": "Approved"}
    _no_clashes(monkeypatch, [approved, pending])
    with pytest.raises(RequestFormError) as exc2:
        run(request_intake.refuse_leave_clashes("2026-11-03", "2026-11-03", "casey@ucsh.com", "7"))
    assert not isinstance(exc2.value, PendingClash)           # approved wins: a plain refusal
    assert "approved time off" in str(exc2.value) and "#61" in str(exc2.value)
