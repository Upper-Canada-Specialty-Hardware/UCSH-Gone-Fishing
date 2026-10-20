"""Approvals route past managers who are on vacation (#342: #369, #370, #371).

The owner's ruling: a request's managers are an ordered list, and the approval
email and text go to ONLY the first manager not off that day. "Off" means an
approved full-day leave covering the day; a part-day or pending leave does not
count. When every manager is off, no one is asked and the request shows on the
admin cover list until a manager is back. The result email names whoever
decided, including a manager who stood in.

All of this runs on the Postgres path, so both storage overrides are pinned to
``postgres`` and ``sp_client`` is faked with an in-memory Staff Directory, exactly
like test_request_submission.py.
"""
import asyncio
import copy
import itertools
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.app_setting import AppSetting
from app.models.balance_ledger import BalanceLedger
from app.models.legacy_finding import LegacyFinding
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.models.sync_run import SyncRun
from app.services import request_decisions as rd
from app.services import request_submission as rs
from app.services import vacation_cover as vc
from staff_seed import import_staff, wipe_staff

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
STAFF = settings.SP_LIST_STAFF_DIRECTORY
USERS = "User Information List"

_item_ids = itertools.count(81_000_000)

# A fixed "today" the routing is tested against, so a manager's leave either
# covers it or does not, deterministically (no dependence on the clock).
TODAY = date(2026, 11, 20)

_BALANCES = {
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 0, "Payout": 0,
}
# Alice lists two managers in order: Boss A first, then Boss B.
EMPLOYEE = {"id": "10", "fields": {
    "Title": "Alice Salary", "EmailAddress": "alice@ucsh.com", "SalaryHourly": "Salary",
    "Location": "Toronto Warden", "Department": "Sales",
    "AllManagers": [
        {"LookupValue": "Boss A", "LookupId": 20},
        {"LookupValue": "Boss B", "LookupId": 30},
    ],
    **_BALANCES,
}}
BOSS_A = {"id": "20", "fields": {
    "Title": "Boss A", "EmailAddress": "bossa@ucsh.com", "CellNumber": "+16475550001",
    "SalaryHourly": "Salary", **_BALANCES,
}}
BOSS_B = {"id": "30", "fields": {
    "Title": "Boss B", "EmailAddress": "bossb@ucsh.com", "CellNumber": "+16475550002",
    "SalaryHourly": "Salary", **_BALANCES,
}}
USER_ITEMS = [{"id": "101", "fields": {"Title": "Alice Salary", "EMail": "alice@ucsh.com"}}]


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


class FakeSharePoint:
    """An in-memory stand-in for sp_client: the Staff Directory, users, and lists."""

    def __init__(self):
        self.lists = {
            STAFF: [copy.deepcopy(EMPLOYEE), copy.deepcopy(BOSS_A), copy.deepcopy(BOSS_B)],
            USERS: [copy.deepcopy(u) for u in USER_ITEMS],
            LEAVE: [],
        }

    async def get_list_items(self, list_id, filter=None, select=None, expand="fields", top=5000):
        """Every item of a list, copied so a caller cannot mutate the store."""
        return [copy.deepcopy(i) for i in self.lists.get(list_id, [])]

    async def get_list_item(self, list_id, item_id):
        """One item by id, or a KeyError (get_employee_by_id swallows it)."""
        for item in self.lists.get(list_id, []):
            if str(item["id"]) == str(item_id):
                return copy.deepcopy(item)
        raise KeyError(item_id)

    async def get_list_item_or_none(self, list_id, item_id):
        """One item by id, or None."""
        for item in self.lists.get(list_id, []):
            if str(item["id"]) == str(item_id):
                return copy.deepcopy(item)
        return None


@pytest.fixture
def sp(monkeypatch):
    """A faked sp_client, both storages on Postgres, a clean db, and a fixed today."""
    fake = FakeSharePoint()
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item", fake.get_list_item)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    # Pin the routing day so a manager's leave covers it (or not) deterministically.
    monkeypatch.setattr(rs, "today_toronto", lambda: TODAY)
    monkeypatch.setattr(vc, "today_toronto", lambda: TODAY)
    run(_wipe())
    run(wipe_staff())
    run(import_staff())
    yield fake
    run(_wipe())
    run(wipe_staff())


async def _wipe():
    """Empty every table these tests touch."""
    async with async_session() as session:
        for model in (BalanceLedger, RequestEvent, Outbox, RequestChange, LegacyFinding,
                      SharepointSeen, RequestRecord, RequestCounter, SyncRun, AppSetting):
            await session.execute(delete(model))
        await session.commit()


async def _leave_record(**fields) -> RequestRecord:
    """Insert one pending leave request for Alice, and return it."""
    base = dict(
        kind="leave", item_id=next(_item_ids), status="pending",
        employee_id="10", employee_email="alice@ucsh.com",
        leave_type="Sick or Personal Day", start_date=date(2026, 12, 1), end_date=date(2026, 12, 1),
        days=2.0, approval_version=1, balance_state="not_applied",
        extra={"RequestSource": "Request page"},
    )
    base.update(fields)
    async with async_session() as session:
        record = RequestRecord(**base)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def _manager_leave(manager_id: str, start: date, end: date,
                         leave_type="Vacation", status="approved") -> None:
    """Give a manager a leave of their own, so they count as off (or not)."""
    async with async_session() as session:
        session.add(RequestRecord(
            kind="leave", item_id=next(_item_ids), status=status,
            employee_id=manager_id, employee_name="Manager",
            leave_type=leave_type, start_date=start, end_date=end,
            days=1.0, approval_version=1, balance_state="not_applied", extra={},
        ))
        await session.commit()


async def _rows(record):
    """Read back a request's events and outbox rows, and the request itself."""
    async with async_session() as session:
        fresh = await session.get(RequestRecord, record.id)
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == record.id)
        )).scalars().all()
        outbox = (await session.execute(
            select(Outbox).where(Outbox.request_id == record.id)
        )).scalars().all()
        return fresh, events, outbox


def _manager_emails(outbox) -> list[str]:
    """The recipients of the queued manager approval emails."""
    return [o.recipient for o in outbox if o.message_type == "manager_approval" and o.channel == "email"]


# ----- #369: the ask goes to the first manager who is not off -----


def test_the_first_manager_off_is_skipped_and_the_second_is_asked(sp):
    # Boss A is on vacation over the routing day; Boss B is not.
    run(_manager_leave("20", TODAY - timedelta(days=2), TODAY + timedelta(days=5)))
    record = run(_leave_record())

    run(rs.submit("leave", record.item_id, channel="request_page"))

    fresh, events, outbox = run(_rows(record))
    # Only Boss B is emailed; Boss A, who is off, is not.
    assert _manager_emails(outbox) == ["bossb@ucsh.com"]
    # The request is now assigned to the stand-in, Boss B.
    assert fresh.manager_id == "30" and fresh.manager_name == "Boss B"
    # The move is on the timeline: Boss A was skipped, with the date they are back.
    reroute = [e for e in events if e.type == "sent_to_manager"]
    assert reroute and reroute[0].detail.get("manager") == "Boss B"
    skipped = reroute[0].detail.get("skipped")
    assert skipped and skipped[0]["manager"] == "Boss A"
    assert skipped[0]["back"] == (TODAY + timedelta(days=6)).isoformat()


def test_a_part_day_leave_does_not_make_a_manager_off(sp):
    # Boss A has a part-day leave over the routing day; it must not skip them.
    run(_manager_leave("20", TODAY, TODAY, leave_type="Half Day or Partial Day Off"))
    record = run(_leave_record())

    run(rs.submit("leave", record.item_id, channel="request_page"))

    fresh, _events, outbox = run(_rows(record))
    assert _manager_emails(outbox) == ["bossa@ucsh.com"]           # the first manager is asked
    assert fresh.manager_id == "20"


def test_a_pending_leave_does_not_make_a_manager_off(sp):
    # Boss A has a leave over the day, but it is only pending: it must not skip them.
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=1), status="pending"))
    record = run(_leave_record())

    run(rs.submit("leave", record.item_id, channel="request_page"))

    fresh, _events, outbox = run(_rows(record))
    assert _manager_emails(outbox) == ["bossa@ucsh.com"]
    assert fresh.manager_id == "20"


# ----- #370: everyone off -> nobody emailed, listed for the admin -----


def test_all_managers_off_emails_nobody_and_lists_the_request(sp):
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=3)))
    run(_manager_leave("30", TODAY - timedelta(days=1), TODAY + timedelta(days=10)))
    record = run(_leave_record())

    run(rs.submit("leave", record.item_id, channel="request_page"))

    _fresh, _events, outbox = run(_rows(record))
    # No manager email or text goes out; the employee still gets a receipt.
    assert _manager_emails(outbox) == []
    assert not [o for o in outbox if o.message_type == "manager_approval"]
    assert [o.message_type for o in outbox] == ["receipt"]
    # The admin cover list shows the request, its managers and when each is back.
    cover = run(vc.list_requests_awaiting_cover())
    mine = [r for r in cover if r["id"] == str(record.item_id)]
    assert len(mine) == 1
    names = [m["name"] for m in mine[0]["managers"]]
    assert names == ["Boss A", "Boss B"]
    assert mine[0]["request_type"] == "leave" and mine[0]["employee_name"] == "Alice Salary"


def test_a_request_leaves_the_cover_list_when_a_manager_is_back(sp):
    # Only Boss A is off, and only Boss B being off too would list it: Boss B is
    # available, so nobody needs to cover and the request is not on the list.
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=3)))
    record = run(_leave_record())
    run(rs.submit("leave", record.item_id, channel="request_page"))

    cover = run(vc.list_requests_awaiting_cover())
    assert [r for r in cover if r["id"] == str(record.item_id)] == []


# ----- #369: a later reminder re-decides and goes back to a returned manager -----


def test_a_reminder_goes_back_to_the_first_manager_once_they_return(sp):
    # Boss A is off only on day one; day two they are back.
    day_one = date(2026, 11, 20)
    day_two = date(2026, 11, 21)
    run(_manager_leave("20", day_one, day_one))                   # a one-day leave, day one only
    record = run(_leave_record())

    # Day one: Boss A is off, so the reminder stands in Boss B.
    run(rs.queue_reminder("leave", record.item_id, today=day_one))
    fresh_one, _e1, outbox_one = run(_rows(record))
    day_one_reminders = [o for o in outbox_one if o.message_type == "reminder"]
    assert [o.recipient for o in day_one_reminders] == ["bossb@ucsh.com"]
    assert fresh_one.manager_id == "30"

    # Day two: Boss A is back, so the reminder goes back to them.
    run(rs.queue_reminder("leave", record.item_id, today=day_two))
    fresh_two, _e2, outbox_two = run(_rows(record))
    day_two_reminders = [o for o in outbox_two if o.message_type == "reminder"
                         and o.idempotency_key.endswith(f"{day_two.isoformat()}:email:bossa@ucsh.com")]
    assert day_two_reminders                                      # Boss A got the day-two reminder
    assert fresh_two.manager_id == "20"                           # and the request is back with Boss A


# ----- #371: the result email names the manager who stood in -----


def test_the_result_email_names_the_stand_in_manager(sp, monkeypatch):
    # Boss A is off, so Boss B is asked and becomes the assigned manager.
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=3)))
    record = run(_leave_record())
    run(rs.submit("leave", record.item_id, channel="request_page"))

    # Approve as Boss B from the manager dashboard; a simple in-memory pot store
    # stands in for the balance backend, as the decision-path tests do.
    class FakePots:
        def __init__(self):
            self._pots = {"sick": 5.0, "make_up": 0.0, "carry_over": 0.0, "vacation": 10.0, "payout": 0.0}

        async def lock_pots(self, session, employee_id):
            return dict(self._pots)

        async def write_pots(self, session, employee_id, new_values):
            self._pots.update(new_values)

    fake = FakePots()

    async def _get():
        return fake

    monkeypatch.setattr(rd, "get_staff_balances", _get)
    run(rd.approve("leave", record.item_id, "Boss B", "manager_dashboard"))

    _fresh, _events, outbox = run(_rows(record))
    result = [o for o in outbox if o.message_type == "result"]
    assert result and "by Boss B" in result[0].body            # the stand-in is named as the approver


def test_the_result_email_names_whichever_manager_actually_decided(sp, monkeypatch):
    # Nobody is off, so Boss A is asked and assigned; Boss B, also on the list,
    # approves from their own dashboard (actor is their staff id, "30").
    record = run(_leave_record())
    run(rs.submit("leave", record.item_id, channel="request_page"))

    class FakePots:
        def __init__(self):
            self._pots = {"sick": 5.0, "make_up": 0.0, "carry_over": 0.0, "vacation": 10.0, "payout": 0.0}

        async def lock_pots(self, session, employee_id):
            return dict(self._pots)

        async def write_pots(self, session, employee_id, new_values):
            self._pots.update(new_values)

    fake = FakePots()

    async def _get():
        return fake

    monkeypatch.setattr(rd, "get_staff_balances", _get)
    run(rd.approve("leave", record.item_id, "30", "manager_dashboard"))

    _fresh, _events, outbox = run(_rows(record))
    result = [o for o in outbox if o.message_type == "result"]
    assert result and "by Boss B" in result[0].body            # the real decider, not the assigned Boss A
    assert "by Boss A" not in result[0].body


# ----- #370: an all-off reminder sends nothing and does not advance the cadence -----


async def _archive(staff_id: str) -> None:
    """Mark one seeded staff row archived, so ``is_archived`` sees it (#119)."""
    from app.models.mixins import utcnow
    from app.models.staff import Staff
    async with async_session() as session:
        row = (await session.execute(
            select(Staff).where(Staff.sp_item_id == str(staff_id))
        )).scalars().first()
        row.archived_at = utcnow()                                # a set timestamp means archived
        await session.commit()


def test_an_all_off_reminder_sends_nothing_and_does_not_advance(sp):
    # Both managers are off over the routing day, so a reminder pass reaches nobody.
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=5)))
    run(_manager_leave("30", TODAY - timedelta(days=1), TODAY + timedelta(days=5)))
    record = run(_leave_record())

    queued = run(rs.queue_reminder("leave", record.item_id, today=TODAY))
    assert queued is False                                        # nothing was queued

    fresh, _events, outbox = run(_rows(record))
    assert [o for o in outbox if o.message_type == "reminder"] == []   # no reminder email went out
    assert not fresh.reminder_count                               # the cadence did not advance (0 or None)
    assert fresh.last_reminder_at is None                         # and the clock was not moved


def test_the_sweep_first_asks_a_returned_manager_for_an_all_off_request(sp, monkeypatch):
    # The request is sent while both managers are off, so nobody is asked at submit.
    # Boss A (first on the list) stays off; Boss B comes back by the sweep day.
    run(_manager_leave("20", TODAY - timedelta(days=1), TODAY + timedelta(days=10)))  # Boss A still off
    run(_manager_leave("30", TODAY - timedelta(days=1), TODAY + timedelta(days=1)))   # Boss B back soon
    record = run(_leave_record())
    run(rs.submit("leave", record.item_id, channel="request_page"))
    _f0, _e0, outbox0 = run(_rows(record))
    assert _manager_emails(outbox0) == []                         # nobody asked at submit (#370)

    # A later sweep day, once Boss B is back: the sweep asks the first available
    # manager now, as a first ask rather than a reminder, without the 30-day wait.
    later = TODAY + timedelta(days=3)
    monkeypatch.setattr(rs, "today_toronto", lambda: later)
    queued = run(rs.queue_due_reminders())
    assert queued == 1                                            # the stand-in was first-asked

    fresh, events, outbox = run(_rows(record))
    assert _manager_emails(outbox) == ["bossb@ucsh.com"]          # the first available manager (Boss B)
    assert fresh.manager_id == "30"                               # the request now points at Boss B
    assert [o for o in outbox if o.message_type == "reminder"] == []     # it is not a reminder
    assert any(o.message_type == "manager_approval" for o in outbox)     # it is a real manager ask
    # The ask is keyed under the version's sent_to_manager step (so it is once),
    # and the reroute past the still-off Boss A is on the timeline.
    approval = [o for o in outbox if o.message_type == "manager_approval"][0]
    assert ":sent_to_manager:" in approval.idempotency_key and later.isoformat() not in approval.idempotency_key
    assert any(e.type == "sent_to_manager" for e in events)


def test_an_archived_submitter_gets_no_manager_reminder(sp):
    # Alice has left; a reminder pass for her pending request reaches no manager and
    # the request simply stays pending (admins still see it) (#119, #368).
    record = run(_leave_record())
    run(_archive("10"))

    queued = run(rs.queue_reminder("leave", record.item_id, today=TODAY))
    assert queued is False

    fresh, _events, outbox = run(_rows(record))
    assert [o for o in outbox if o.message_type in ("reminder", "manager_approval")] == []
    assert fresh.status == "pending"                              # still pending, just not reminded
