"""New requests from the real HTTP entry points, once requests + staff are in Postgres (#324).

Each test drives a FastAPI TestClient over the public forms endpoints, the admin
edit endpoint and the approval link route, with sp_client (staff + holidays) and
the email/SMS senders faked and both storage settings pinned to postgres. Nothing
starts from a pre-created row: a request is created by POSTing a form, and it is
decided by POSTing an approval link. The checks are: a leave, an overtime and a
carry-over each write one typed row, one submitted event and the queued outbox
messages; overtime worked on a company holiday is auto-rejected with no manager
message; an unknown submitter is refused with no row; and an admin edit bumps the
approval version so the old link is stale while the new one applies the balance
exactly once.
"""
import asyncio
import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models import RequestApprovalState
from app.models.app_setting import AppSetting
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.repositories.staff_balances import POT_COLUMNS
from app.routes.approval import router as approval_router
from app.routes.dashboard import router as dashboard_router
from app.routes.forms import router as forms_router
from app.services import request_decisions as rd
from app.services import request_intake
from app.services.approval_links import NO_EXPIRY, _sign

STAFF = settings.SP_LIST_STAFF_DIRECTORY
USERS = "User Information List"
LEAVE = settings.SP_LIST_LEAVE_REQUESTS
OVERTIME = settings.SP_LIST_OVERTIME_REQUESTS
CARRYOVER = settings.SP_LIST_CARRYOVER_PAYOUT

# A full pot set, so the real balance projections have every pot they read.
_BALANCES = {
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 0, "Payout": 0,
}
# Alice (the submitter), who lists Boss Person as her manager; Boss has a cell.
EMPLOYEE = {"id": "10", "fields": {
    "Title": "Alice Salary", "EmailAddress": "alice@ucsh.com", "SalaryHourly": "Salary",
    "Location": "Toronto Warden", "Department": "Sales",
    "AllManagers": [{"LookupValue": "Boss Person", "LookupId": 20}], **_BALANCES,
}}
MANAGER = {"id": "20", "fields": {
    "Title": "Boss Person", "EmailAddress": "boss@ucsh.com", "CellNumber": "+16475551234",
    "SalaryHourly": "Salary", **_BALANCES,
}}


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


class FakeSharePoint:
    """An in-memory sp_client: the Staff Directory and the (empty) user list."""

    def __init__(self):
        self.lists = {
            STAFF: [copy.deepcopy(EMPLOYEE), copy.deepcopy(MANAGER)],
            USERS: [],
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


async def _wipe():
    """Empty every table these tests touch, for isolation."""
    async with async_session() as session:
        for model in (BalanceLedger, RequestEvent, Outbox, RequestChange, RequestApprovalState,
                      RequestRecord, RequestCounter, AppSetting):
            await session.execute(delete(model))
        await session.commit()


@pytest.fixture
def client(monkeypatch):
    """A TestClient over the forms, dashboard and approval routers, Postgres-pinned.

    Fakes sp_client (staff only), the holiday calendar (no holidays, a mappable
    province), an open carry-over window, and both storage overrides at postgres,
    so a POSTed form records a typed row and queues (never sends) its messages.
    """
    fake = FakeSharePoint()
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item", fake.get_list_item)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")   # requests live in Postgres
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")  # and staff, so the Postgres paths run

    # No holidays, and a province the location maps to, so the submit-time holiday
    # check runs but finds nothing (the holiday test overrides is_company_holiday).
    async def no_holidays(province):
        return []
    monkeypatch.setattr("app.services.holidays.get_holidays_for_province", no_holidays)
    monkeypatch.setattr("app.services.employee.map_location_to_province", lambda location: "ON")

    # The admin opened the carry-over/payout window around today (#176).
    async def open_window():
        from datetime import date
        return date(2000, 1, 1), date(2999, 12, 31)
    monkeypatch.setattr(request_intake, "carryover_payout_window", open_window)

    run(_wipe())
    app = FastAPI()
    app.include_router(forms_router, prefix="/api/forms")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(approval_router, prefix="/api")
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    run(_wipe())


async def _records(kind):
    """Every typed request row of a kind, oldest first."""
    async with async_session() as session:
        return (await session.execute(
            select(RequestRecord).where(RequestRecord.kind == kind).order_by(RequestRecord.item_id)
        )).scalars().all()


async def _events_and_outbox(request_id):
    """The events and outbox rows for one request's internal id."""
    async with async_session() as session:
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == request_id)
        )).scalars().all()
        outbox = (await session.execute(
            select(Outbox).where(Outbox.request_id == request_id)
        )).scalars().all()
        return events, outbox


async def _count(model):
    """How many rows a table holds."""
    async with async_session() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ----- each app entry point writes one valid row and queues its messages -----


def test_a_leave_from_the_form_endpoint_records_and_queues(client):
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-04",
        "first_name": "Alice", "last_name": "Salary", "submitter_email": "alice@ucsh.com",
    })

    assert resp.status_code == 200, resp.text
    records = run(_records("leave"))
    assert len(records) == 1                                      # exactly one typed row
    record = records[0]
    assert record.employee_id == "10" and record.manager_id == "20"   # identity stamped, never null
    events, outbox = run(_events_and_outbox(record.id))
    assert len([e for e in events if e.type == "submitted"]) == 1
    kinds = sorted((o.message_type, o.channel) for o in outbox)
    assert kinds == [("manager_approval", "email"), ("manager_approval", "sms"), ("receipt", "email")]
    assert all(o.status == "pending" for o in outbox)


def test_an_overtime_from_the_form_endpoint_records_and_queues(client):
    resp = client.post("/api/forms/overtime", json={
        "description": "Inventory count", "date": "2026-11-02", "hours": 4,
        "submitter_email": "alice@ucsh.com",
    })

    assert resp.status_code == 200, resp.text
    records = run(_records("overtime"))
    assert len(records) == 1 and records[0].employee_id == "10"
    events, outbox = run(_events_and_outbox(records[0].id))
    assert len([e for e in events if e.type == "submitted"]) == 1
    kinds = sorted((o.message_type, o.channel) for o in outbox)
    assert kinds == [("manager_approval", "email"), ("manager_approval", "sms"), ("receipt", "email")]


def test_a_carryover_from_the_form_endpoint_records_and_queues(client):
    resp = client.post("/api/forms/carryover-payout", json={
        "type_of_request": "Carry Over", "days": 3, "submitter_email": "alice@ucsh.com",
    })

    assert resp.status_code == 200, resp.text
    records = run(_records("carryover_payout"))
    assert len(records) == 1 and records[0].employee_id == "10"
    events, outbox = run(_events_and_outbox(records[0].id))
    assert len([e for e in events if e.type == "submitted"]) == 1
    kinds = sorted((o.message_type, o.channel) for o in outbox)
    assert kinds == [("manager_approval", "email"), ("manager_approval", "sms"), ("receipt", "email")]


# ----- a submit-time auto-rejection: nothing reaches the manager -----


def test_overtime_on_a_holiday_is_rejected_and_tells_only_the_employee(client, monkeypatch):
    # The date worked is a company holiday, so the overtime is auto-rejected at
    # submit, exactly as the SharePoint intake does, before any manager is asked.
    monkeypatch.setattr(
        "app.services.holidays.is_company_holiday",
        lambda day, holidays: (True, "Remembrance Day"),
    )

    resp = client.post("/api/forms/overtime", json={
        "description": "Holiday shift", "date": "2026-11-11", "hours": 4,
        "submitter_email": "alice@ucsh.com",
    })

    assert resp.status_code == 200, resp.text
    records = run(_records("overtime"))
    assert len(records) == 1 and records[0].status == "rejected"  # decided at submit, not pending
    events, outbox = run(_events_and_outbox(records[0].id))
    types = {e.type for e in events}
    assert "submitted" in types and "rejected" in types
    # One message, to the employee; nothing was queued to the manager.
    assert [(o.message_type, o.channel, o.recipient) for o in outbox] == [
        ("rejection", "email", "alice@ucsh.com"),
    ]
    assert not any(o.message_type == "manager_approval" for o in outbox)


def test_a_payout_over_the_cap_is_rejected_at_submit(client):
    # A 6-day payout exceeds the 5-day cap, so it is auto-rejected before the
    # manager is asked, with only the employee told.
    resp = client.post("/api/forms/carryover-payout", json={
        "type_of_request": "Payout", "days": 6, "submitter_email": "alice@ucsh.com",
    })

    assert resp.status_code == 200, resp.text
    records = run(_records("carryover_payout"))
    assert len(records) == 1 and records[0].status == "rejected"
    _, outbox = run(_events_and_outbox(records[0].id))
    assert [(o.message_type, o.channel) for o in outbox] == [("rejection", "email")]


# ----- an unknown submitter is refused, and no row is written -----


def test_an_unknown_submitter_is_refused_with_no_row(client):
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-04",
        "first_name": "Ghost", "last_name": "Nobody", "submitter_email": "ghost@ucsh.com",
    })

    assert resp.status_code == 400                                # refused with a clear message
    assert "ghost@ucsh.com" in resp.json()["detail"]
    assert run(_count(RequestRecord)) == 0                        # nothing was written


# ----- an admin edit bumps the version: old link stale, new link applies once -----


class FakeStaffBalances:
    """An in-memory StaffBalances: holds pots in a dict, records every write."""

    def __init__(self, pots):
        self._pots = dict(pots)
        self.write_calls = []

    async def lock_pots(self, session, employee_id):
        """Return a copy of the current pots."""
        return dict(self._pots)

    async def write_pots(self, session, employee_id, new_values):
        """Apply a pot write and remember it."""
        self.write_calls.append(dict(new_values))
        self._pots.update(new_values)


def _approval_form(request_type, action, request_id, version, mgr="20"):
    """The POST body for an approval link at a given version (a valid signed token)."""
    token = _sign(request_type, request_id, action, mgr, str(NO_EXPIRY), version)
    return {"token": token, "mgr": mgr, "exp": str(NO_EXPIRY), "v": str(version)}


def test_an_admin_edit_makes_the_old_link_stale_and_the_new_link_apply_once(client, monkeypatch):
    # A full set of pots the approval moves against; the leave spends Sick.
    fake = FakeStaffBalances({pot: 0.0 for pot in POT_COLUMNS} | {"sick": 5.0, "vacation": 10.0})

    async def _get():
        return fake
    monkeypatch.setattr(rd, "get_staff_balances", _get)          # the decision path reads this

    # 1. Submit a sick leave from the form endpoint (version 1).
    created = client.post("/api/forms/leave", json={
        "leave_type": "Sick or Personal Day", "start_date": "2026-11-02", "end_date": "2026-11-02",
        "first_name": "Alice", "last_name": "Salary", "submitter_email": "alice@ucsh.com",
    })
    assert created.status_code == 200, created.text
    item_id = created.json()["item_id"]

    # 2. An admin edits the days; the version bumps and a fresh link is queued.
    edited = client.post(f"/api/dashboard/admin/edit/leave/{item_id}", json={
        "Days": 3, "LeaveType": "Sick or Personal Day",
        "StartDate": "2026-11-02", "EndDate": "2026-11-02", "reason": "Corrected the day count",
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["new_version"] == 2
    record = run(_records("leave"))[0]
    assert any(e.type == "edited" for e in run(_events_and_outbox(record.id))[0])

    # 3. The link from the first email (version 1) is now out of date.
    old = client.post(f"/api/leave/approve/{item_id}",
                      data=_approval_form("leave", "approve", item_id, version=1))
    assert old.status_code == 200
    assert "more recent email" in old.text                       # the outdated page, not an approval
    assert fake.write_calls == []                                # no balance moved by the stale link
    assert run(_records("leave"))[0].status == "pending"         # still awaiting a current decision

    # 4. The link from the edit email (version 2) approves it, moving Sick once.
    new = client.post(f"/api/leave/approve/{item_id}",
                      data=_approval_form("leave", "approve", item_id, version=2))
    assert new.status_code == 200
    assert "approved" in new.text.lower()
    assert fake.write_calls == [{"sick": 2.0}]                   # 5 - 3 days, applied exactly once
    assert run(_records("leave"))[0].status == "approved"
