"""Finishing a new request: the submitted event, the queued messages, reminders (#323-#326).

All of this runs only once requests AND staff both live in Postgres, so each test
pins both storage overrides to ``postgres`` and fakes ``sp_client`` with an
in-memory Staff Directory, so a request is recorded and its notifications are
QUEUED (never sent) against the SQLite test database. The checks are: a submission
writes one request, one submitted event and three outbox rows and sends nothing;
a repeat adds nothing; the sender turns a manager row into ``sent_to_manager`` and
a receipt row into ``receipt_sent``; a reminder is queued at most once a day; and a
new SharePoint item found after the switch is flagged once (never imported) across
repeated checks and a webhook for the same item.
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
from app.services import request_submission as rs
from app.tasks import outbox_worker
from staff_seed import import_staff, wipe_staff

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
STAFF = settings.SP_LIST_STAFF_DIRECTORY
USERS = "User Information List"

_item_ids = itertools.count(80_000_000)

# A full employee balance set, so the real projection functions have every pot.
_BALANCES = {
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 0, "Payout": 0,
}
# The employee (Alice), who lists Boss Person as her manager; Boss has a cell.
EMPLOYEE = {"id": "10", "fields": {
    "Title": "Alice Salary", "EmailAddress": "alice@ucsh.com", "SalaryHourly": "Salary",
    "Location": "Toronto Warden", "Department": "Sales",
    "AllManagers": [{"LookupValue": "Boss Person", "LookupId": 20}], **_BALANCES,
}}
MANAGER = {"id": "20", "fields": {
    "Title": "Boss Person", "EmailAddress": "boss@ucsh.com", "CellNumber": "+16475551234",
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
            STAFF: [copy.deepcopy(EMPLOYEE), copy.deepcopy(MANAGER)],
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
    """A faked sp_client, both storages pinned to Postgres, and a clean database."""
    fake = FakeSharePoint()
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item", fake.get_list_item)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")   # requests live in Postgres
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")  # and so do staff, so decisions_active is true
    run(_wipe())
    run(wipe_staff())
    run(import_staff())                                             # staff live in Postgres: copy the fake in
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
    """Insert one pending leave request already linked to Alice, and return it."""
    base = dict(
        kind="leave", item_id=next(_item_ids), status="pending",
        employee_id="10", employee_email="alice@ucsh.com",
        leave_type="Sick or Personal Day", start_date=date(2026, 11, 2), end_date=date(2026, 11, 2),
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


async def _mark_asked(record):
    """Give an app-native request a manager_approval outbox row, as a submit does.

    A pending request on the reminder cadence was asked at submit and so carries a
    manager_approval outbox row keyed under its version's sent_to_manager step.
    ``_leave_record`` writes no such row, so a test that wants the reminder path
    (not the all-off first ask, #370) adds one here.
    """
    from app.services.request_journal import step_key
    key = step_key(record.kind, record.item_id, "sent_to_manager", record.approval_version)
    async with async_session() as session:
        session.add(Outbox(
            request_id=record.id, message_type="manager_approval", channel="email",
            recipient="boss@ucsh.com", subject="x", body="x", status="sent", attempts=1,
            idempotency_key=f"{key}:email:boss@ucsh.com",
        ))
        await session.commit()


async def _rows(record):
    """Read back a request's events and outbox rows."""
    async with async_session() as session:
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == record.id)
        )).scalars().all()
        outbox = (await session.execute(
            select(Outbox).where(Outbox.request_id == record.id)
        )).scalars().all()
        return events, outbox


# ----- a request-page submission records and queues, and sends nothing -----


def test_a_submission_writes_one_event_and_three_queued_messages(sp):
    record = run(_leave_record())

    recorded = run(rs.submit("leave", record.item_id, channel="request_page"))

    assert recorded is True
    events, outbox = run(_rows(record))
    submitted = [e for e in events if e.type == "submitted"]
    assert len(submitted) == 1 and submitted[0].channel == "request_page"
    # Three rows: the manager email, the manager text, and the employee receipt.
    kinds = sorted((o.message_type, o.channel) for o in outbox)
    assert kinds == [("manager_approval", "email"), ("manager_approval", "sms"), ("receipt", "email")]
    # Nothing has been sent: every row is still pending for the worker to deliver.
    assert all(o.status == "pending" for o in outbox)
    # The identity the decision path relies on was stamped from the staff record.
    async def reread():
        async with async_session() as session:
            return await session.get(RequestRecord, record.id)
    fresh = run(reread())
    assert fresh.employment_type == "Salary" and fresh.manager_id == "20"
    assert fresh.manager_name == "Boss Person" and fresh.submitted_channel == "request_page"
    # The manager email carries a working approval link at the current version.
    manager_email = next(o for o in outbox if o.message_type == "manager_approval" and o.channel == "email")
    assert f"/api/leave/approve/{record.item_id}" in manager_email.body and "v=1" in manager_email.body


def test_a_second_identical_submission_adds_nothing(sp):
    record = run(_leave_record())

    run(rs.submit("leave", record.item_id, channel="request_page"))
    run(rs.submit("leave", record.item_id, channel="request_page"))

    events, outbox = run(_rows(record))
    assert len([e for e in events if e.type == "submitted"]) == 1   # one submitted event only
    assert len(outbox) == 3                                         # the same three rows, not six


# ----- the sender turns each message type into the right event -----


def test_the_sender_marks_sent_to_manager_and_receipt_sent(monkeypatch, sp):
    record = run(_leave_record())
    run(rs.submit("leave", record.item_id, channel="request_page"))

    async def fake_email(recipients, subject, body):
        return None

    async def fake_sms(to, body):
        return {"sid": "SM-1"}

    monkeypatch.setattr("app.graph.email.send_email", fake_email)
    monkeypatch.setattr("app.services.sms.send_sms", fake_sms)

    run(outbox_worker._drain())                                     # deliver the three queued rows

    events, outbox = run(_rows(record))
    assert all(o.status == "sent" for o in outbox)
    seen = {e.type for e in events}
    assert "sent_to_manager" in seen and "receipt_sent" in seen
    # The manager event names the channel and the manager, per the contract.
    manager_event = next(e for e in events if e.type == "sent_to_manager")
    assert manager_event.detail.get("manager") == "Boss Person"
    assert manager_event.detail.get("to") in ("email", "sms")


# ----- reminders: queued at most once per day -----


def test_a_reminder_is_queued_at_most_once_a_day(sp):
    record = run(_leave_record())
    today = date(2026, 11, 20)
    tomorrow = today + timedelta(days=1)

    run(rs.queue_reminder("leave", record.item_id, today=today))
    run(rs.queue_reminder("leave", record.item_id, today=today))   # same day: no new row
    run(rs.queue_reminder("leave", record.item_id, today=tomorrow))

    _, outbox = run(_rows(record))
    reminders = [o for o in outbox if o.message_type == "reminder"]
    # One reminder email per manager per day: one today, one tomorrow.
    assert len(reminders) == 2
    assert all(o.channel == "email" for o in reminders)            # reminders are email-only



# ----- Postgres reminders keep the cutoff and the reminders_closed flag (finding 12) -----


def test_postgres_reminders_respect_the_cutoff_and_the_closed_flag(sp, monkeypatch):
    """Carry-over/payout stop at year-end and at a max count, and a closed flag is honoured."""
    from datetime import datetime, timezone

    from app.models import RequestApprovalState
    from app.models.mixins import utcnow

    due_at = utcnow() - timedelta(days=40)                         # older than the 30-day first threshold
    last_year = datetime(date.today().year - 1, 6, 1, tzinfo=timezone.utc)

    # 1) A due, in-window leave: its start date is in the future, so it is offered.
    due_leave = run(_leave_record(item_id=90_000_101, created_at=due_at))
    run(_mark_asked(due_leave))                                    # asked at submit: on the reminder cadence
    # 2) A carry-over made last year: past its year-end, so reminders must stop.
    co_old = run(_leave_record(
        item_id=90_000_102, kind="carryover_payout", leave_type=None,
        request_option="carry_over", days=3.0, start_date=None, end_date=None,
        created_at=last_year,
    ))
    # 3) A payout made this year but already reminded the maximum number of times.
    co_maxed = run(_leave_record(
        item_id=90_000_103, kind="carryover_payout", leave_type=None,
        request_option="payout", days=2.0, start_date=None, end_date=None,
        created_at=due_at, reminder_count=rs.MAX_REMINDERS_WITHOUT_DATE,
    ))
    # 4) A due leave whose approval-version state says reminders are closed.
    closed_leave = run(_leave_record(item_id=90_000_104, created_at=due_at))

    async def seed_states():
        async with async_session() as session:
            session.add(RequestApprovalState(                      # the old carry-over has an open state row
                list_id=settings.SP_LIST_CARRYOVER_PAYOUT, item_id=str(co_old.item_id),
                current_version=1, current_snapshot={}, reminders_closed=False,
            ))
            session.add(RequestApprovalState(                      # the due leave is explicitly closed
                list_id=settings.SP_LIST_LEAVE_REQUESTS, item_id=str(closed_leave.item_id),
                current_version=1, current_snapshot={}, reminders_closed=True,
            ))
            await session.commit()

    run(seed_states())

    offered: list = []

    async def fake_queue(session, kind, item_id, today):
        offered.append((kind, item_id))                            # record who was actually offered a reminder
        return True

    monkeypatch.setattr(rs, "_queue_reminder", fake_queue)
    run(rs.queue_due_reminders())

    # Only the due, in-window leave is reminded; the two cutoff ones and the
    # closed one are skipped.
    assert offered == [("leave", due_leave.item_id)]

    async def state_closed(item_id):
        async with async_session() as session:
            row = await session.get(
                RequestApprovalState, (settings.SP_LIST_CARRYOVER_PAYOUT, str(item_id))
            )
            return row.reminders_closed

    # The past-cutoff carry-over had its state row closed so later scans skip it.
    assert run(state_closed(co_old.item_id)) is True
    assert co_maxed.item_id not in [i for _k, i in offered]


# ----- an edit holds the version state with a naive timestamp (finding 3) -----


def test_an_edit_holds_the_version_state_with_a_naive_timestamp():
    # request_approval_state.last_emailed_at is a naive DateTime column, so an
    # aware value would make asyncpg reject the whole edit in production. The
    # held timestamp must be naive UTC, matching what approval_versions writes.
    from app.models import RequestApprovalState
    from app.services.approval_versions import MATERIAL_FIELDS_LEAVE

    before = {k: "x" for k in MATERIAL_FIELDS_LEAVE}
    after = {**before, "Days": 2}
    item_id = next(_item_ids)
    list_id = rs._edit_list_id("leave")

    async def go():
        async with async_session() as session:
            await rs._sync_version_state(session, "leave", item_id, 2, before, after)
            await session.commit()
        async with async_session() as session:
            return await session.get(RequestApprovalState, (list_id, str(item_id)))

    state = run(go())
    assert state is not None and state.current_version == 2
    assert state.last_emailed_at is not None
    assert state.last_emailed_at.tzinfo is None                   # naive UTC, so asyncpg accepts the edit


# ----- the switch is silent: imported requests continue the old reminder cadence -----


def test_imported_requests_keep_their_old_reminder_cadence(sp, monkeypatch):
    """An old pending request is not reminded just because it moved to Postgres.

    Imported rows carry no reminder history of their own; the old path kept it in
    request_approval_state. The first sweep after the switch must continue that
    clock, not restart it from the submission date, and must leave alone a request
    the old path never emailed a manager about.
    """
    from app.models import RequestApprovalState
    from app.models.mixins import utcnow

    long_ago = utcnow() - timedelta(days=200)                      # submitted long before the switch
    def naive(moment):
        """The state table stores naive UTC."""
        return moment.replace(tzinfo=None)

    # 1) Imported, reminded 3 days ago by the old path: not due for 4 more days.
    recent = run(_leave_record(item_id=90_000_201, sp_item_id="90000201", created_at=long_ago))
    # 2) Imported, never reminded, approval email sent 10 days ago: first reminder waits for day 30.
    fresh = run(_leave_record(item_id=90_000_202, sp_item_id="90000202", created_at=long_ago))
    # 3) Imported, last reminded 8 days ago: the weekly repeat is due.
    weekly = run(_leave_record(item_id=90_000_203, sp_item_id="90000203", created_at=long_ago))
    # 4) Imported, but the old path never emailed a manager (no state row): stays silent.
    unseen = run(_leave_record(item_id=90_000_204, sp_item_id="90000204", created_at=long_ago))
    # 5) Made in the app after the switch, 40 days old: reminded on its own clock as before.
    app_made = run(_leave_record(item_id=90_000_205, created_at=utcnow() - timedelta(days=40)))
    run(_mark_asked(app_made))                                    # asked at submit: on the reminder cadence

    async def seed_states():
        async with async_session() as session:
            await session.execute(delete(RequestApprovalState).where(   # a rerun on the same test db starts clean
                RequestApprovalState.item_id.in_([str(r.item_id) for r in (recent, fresh, weekly, unseen)])
            ))
            for record, days_ago, count in ((recent, 3, 2), (fresh, 10, 0), (weekly, 8, 1)):
                session.add(RequestApprovalState(
                    list_id=settings.SP_LIST_LEAVE_REQUESTS, item_id=str(record.item_id),
                    current_version=1, current_snapshot={}, reminders_closed=False,
                    last_emailed_at=naive(utcnow() - timedelta(days=days_ago)), reminder_count=count,
                ))
            await session.commit()

    run(seed_states())

    offered: list = []

    async def fake_queue(session, kind, item_id, today):
        offered.append(item_id)                                    # who was actually offered a reminder
        return True

    monkeypatch.setattr(rs, "_queue_reminder", fake_queue)
    run(rs.queue_due_reminders())

    assert weekly.item_id in offered                               # the old weekly cadence continues
    assert app_made.item_id in offered                             # an app request is unchanged
    assert recent.item_id not in offered                           # not restarted from the submission date
    assert fresh.item_id not in offered                            # first reminder still 30 days from the email
    assert unseen.item_id not in offered                           # never emailed before, so never reminded now
