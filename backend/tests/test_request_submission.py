"""Finishing a new request: the submitted event, the queued messages, reminders (#323-#326).

All of this runs only once requests AND staff both live in Postgres, so each test
pins both storage overrides to ``postgres`` and fakes ``sp_client`` with an
in-memory Staff Directory, so a request is recorded and its notifications are
QUEUED (never sent) against the SQLite test database. The checks are: a submission
writes one request, one submitted event and three outbox rows and sends nothing;
a repeat adds nothing; the sender turns a manager row into ``sent_to_manager`` and
a receipt row into ``receipt_sent``; a reminder is queued at most once a day; and a
Microsoft Form item found after the switch is submitted once across repeated checks
and a webhook for the same item.
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
    yield fake
    run(_wipe())


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


# ----- a Microsoft Form item found after the switch is submitted once -----


def test_a_form_item_is_submitted_once_across_checks_and_a_webhook(sp):
    from app.services.sharepoint_check import run_sharepoint_check, run_sharepoint_check_for_item

    # A Form submission: blank RequestSource, named by its site lookup id.
    sp.lists[LEAVE] = [{
        "id": "500", "createdDateTime": "2026-05-01T10:00:00Z",
        "lastModifiedDateTime": "2026-05-01T10:00:00Z", "eTag": "v1",
        "fields": {"LeaveType": "Sick or Personal Day", "StartDate": "2026-11-02",
                   "EndDate": "2026-11-02", "Days": 2, "Status": "Pending",
                   "SubmittedTestLookupId": 101, "RequestSource": ""},
    }]

    run(run_sharepoint_check_for_item(LEAVE, "500"))               # the webhook handles it at once
    run(run_sharepoint_check())                                    # the scheduled sweep: no duplicate
    run(run_sharepoint_check())

    async def read():
        async with async_session() as session:
            record = (await session.execute(
                select(RequestRecord).where(RequestRecord.kind == "leave", RequestRecord.sp_item_id == "500")
            )).scalars().first()
            events = (await session.execute(
                select(RequestEvent).where(RequestEvent.request_id == record.id)
            )).scalars().all()
            outbox = (await session.execute(
                select(Outbox).where(Outbox.request_id == record.id)
            )).scalars().all()
            return record, events, outbox

    record, events, outbox = run(read())
    assert record is not None and record.manager_id == "20"        # the manager was assigned
    assert len([e for e in events if e.type == "submitted"]) == 1  # submitted once, on the form channel
    assert {e.channel for e in events if e.type == "submitted"} == {"form"}
    kinds = sorted((o.message_type, o.channel) for o in outbox)
    assert kinds == [("manager_approval", "email"), ("manager_approval", "sms"), ("receipt", "email")]
