"""After the switch to Postgres nothing in the app may touch SharePoint.

The owner's ruling (2026-10-18): once the storage switch has run, Postgres is the
only source of truth for requests, staff and holidays. No pipeline may read or
write SharePoint, there is no fallback to SharePoint when Postgres misses, and
no SharePoint read comes first with Postgres as the fallback.

These tests put the app in the real switched state (the three storage settings
set to Postgres in the database, not an environment override), with NO faked
``sp_client``: any SharePoint call reaches ``GraphClient._request``, whose
cut-off gate is wrapped here to record the call before refusing it. Fetching a
Graph token fails the test too. Each test then drives one pipeline end to end
and asserts the recorder stayed empty, so a call site that swallowed the
refusal still fails the test.

Emails and texts are dropped at their real chokepoints (every recipient refused),
so nothing leaves the test either.
"""
import asyncio
import copy
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph import auth as graph_auth
from app.graph import sharepoint
from app.main import app
from app.models.app_setting import AppSetting
from app.models.balance_ledger import BalanceLedger
from app.models.carryover_reset_log import CarryoverResetLog
from app.models.holiday import Holiday
from app.models.legacy_finding import LegacyFinding
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.models.sync_run import SyncRun
from app.routes.dashboard import DashboardUser, get_dashboard_user
from app.services import rehearsal, sharepoint_cutoff
from app.services.admin_auth import admin_guard
from app.services.app_settings import POSTGRES
from staff_seed import import_staff, wipe_staff

STAFF = settings.SP_LIST_STAFF_DIRECTORY

_BALANCES = {
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 0, "Payout": 0,
}
# Alice (employee 10) reports to Boss Person (manager 20); both salaried in Ontario.
EMPLOYEE = {"id": "10", "fields": {
    "Title": "Alice Salary", "EmailAddress": "alice@ucsh.com", "SalaryHourly": "Salary",
    "Location": "Toronto Warden", "Department": "Sales", "CellNumber": "+16475550000",
    # SharePoint names the manager by their Microsoft 365 user id (612), not their
    # staff id (20); the switch converts it. The supervisor link has an id only,
    # and the Supervisor text column names the same person.
    "AllManagers": [{"LookupValue": "Boss Person", "LookupId": 612}],
    "SupervisorLinkLookupId": 612, "Supervisor": "Boss Person", **_BALANCES,
}}
MANAGER = {"id": "20", "fields": {
    "Title": "Boss Person", "EmailAddress": "boss@ucsh.com", "CellNumber": "+16475551234",
    "SalaryHourly": "Salary", "Location": "Toronto Warden", "Department": "Sales", **_BALANCES,
}}


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


async def _wipe():
    """Empty every request, holiday and settings table these tests touch."""
    async with async_session() as session:
        for model in (BalanceLedger, RequestEvent, Outbox, RequestChange, LegacyFinding,
                      SharepointSeen, RequestRecord, RequestCounter, SyncRun, Holiday, AppSetting,
                      CarryoverResetLog):
            await session.execute(delete(model))
        await session.commit()


@pytest.fixture
def switched(monkeypatch):
    """The app as it stands after the switch, with every SharePoint call recorded.

    Staff are imported from a faked Staff Directory first (as the final sync
    does), then the fake is removed, so from the test's first step on only the
    real ``sp_client`` exists and every call it makes hits the recorder.

    Yields:
        The list of refused SharePoint calls, as (method, path) tuples.
    """
    # Make sure no environment override decides the backend: the DB settings do.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "sharepoint")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    run(_wipe())
    run(wipe_staff())

    # The final sync's staff copy, from a fake list that exists only for this step.
    with monkeypatch.context() as seed:
        async def fake_items(list_id, **kwargs):
            return [copy.deepcopy(EMPLOYEE), copy.deepcopy(MANAGER)] if list_id == STAFF else []
        seed.setattr(sharepoint.sp_client, "get_list_items", fake_items)
        run(import_staff())
    from app.services import storage_switch
    run(storage_switch._flip_settings_to_postgres())        # the real flip: settings plus the link conversion

    # Record every SharePoint call before the real gate refuses it.
    calls: list[tuple[str, str]] = []
    real_refuse = sharepoint_cutoff.refuse_if_cut_off

    async def recording_refuse(method, path):
        calls.append((method, path))                        # any call at all is a failure
        await real_refuse(method, path)                     # and the real gate still refuses it
    monkeypatch.setattr(sharepoint_cutoff, "refuse_if_cut_off", recording_refuse)

    async def no_token():
        raise AssertionError("a Microsoft token was requested after the switch")
    monkeypatch.setattr(graph_auth.token_manager, "get_token", no_token)

    # Drop every email and text at the real chokepoints, without rehearsal's
    # SharePoint write block (which would hide a SharePoint write from the gate).
    monkeypatch.setattr(rehearsal, "rehearsal_enabled", lambda: True)
    monkeypatch.setattr(rehearsal, "allow_recipient", lambda _who: False)
    monkeypatch.setattr(rehearsal, "sharepoint_writes_allowed", lambda: True)

    yield calls
    app.dependency_overrides.clear()
    run(_wipe())
    run(wipe_staff())


def _client(role: str | None = None, uid: str = "") -> TestClient:
    """A TestClient on the real app, signed in as an admin and optionally a dashboard user.

    Args:
        role: "employee" or "manager" to answer /me or /team calls as that user.
        uid: The staff id of that user.

    Returns:
        A client that does not run the app's startup (no migrations, no tasks).
    """
    app.dependency_overrides[admin_guard] = lambda: None   # the admin password is not under test
    if role:
        app.dependency_overrides[get_dashboard_user] = lambda: DashboardUser(role=role, user_id=uid)
    return TestClient(app, raise_server_exceptions=False)


def _get_routes(prefix: str) -> list[str]:
    """Every parameter-free GET path under a prefix, so new screens are swept too.

    Read from the app's OpenAPI schema, which lists every mounted path with its
    methods whatever way the routers were included.

    Args:
        prefix: The path prefix, e.g. "/api/dashboard/admin".

    Returns:
        The matching paths, sorted.
    """
    paths = set()
    for path, methods in app.openapi()["paths"].items():
        if "get" in methods and path.startswith(prefix):
            if "{" not in path and not path.endswith("/events"):   # the live stream never ends
                paths.add(path)
    return sorted(paths)


async def _record(kind: str, item_id: int) -> RequestRecord:
    """One request row by kind and app number."""
    async with async_session() as session:
        return (await session.execute(
            select(RequestRecord).where(RequestRecord.kind == kind, RequestRecord.item_id == item_id)
        )).scalar_one()


def _future_weekday(days_ahead: int) -> date:
    """A weekday at least ``days_ahead`` days from today, for a valid leave date."""
    day = date.today() + timedelta(days=days_ahead)
    while day.weekday() >= 5:                               # skip Saturday and Sunday
        day += timedelta(days=1)
    return day


# ----- the gate itself -----


def test_the_gate_refuses_any_graph_call_after_the_switch(switched):
    # Even a direct sp_client read is refused, with no token fetched.
    with pytest.raises(sharepoint_cutoff.SharePointCutOff):
        run(sharepoint.sp_client.get_list_items(STAFF))
    assert len(switched) == 1 and switched[0][0] == "GET"   # recorded once, then refused


def test_the_gate_allows_sharepoint_before_the_switch(monkeypatch):
    # Before the switch the gate stays open (the shadow sync and final sync read SharePoint).
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    run(_wipe())
    assert run(sharepoint_cutoff.sharepoint_cut_off()) is False


# ----- requests: submit, decide, refund, remind, send -----


def test_leave_submit_approve_refund_never_touch_sharepoint(switched):
    client = _client()
    start = _future_weekday(20).isoformat()
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": start, "end_date": start,
        "notes": "family trip", "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code == 200, resp.text
    item_id = int(resp.json().get("id") or resp.json().get("item_id"))

    from app.services.leave_requests import approve_leave_request, refund_leave_request
    result = run(approve_leave_request(item_id, 20, channel="email_link"))
    assert "error" not in result, result
    result = run(refund_leave_request(item_id, "admin", channel="admin_dashboard"))
    assert "error" not in result, result
    assert run(_record("leave", item_id)).status == "refunded"
    assert switched == []                                   # not one SharePoint call


def test_leave_reject_never_touches_sharepoint(switched):
    client = _client()
    start = _future_weekday(25).isoformat()
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": start, "end_date": start,
        "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code == 200, resp.text
    item_id = int(resp.json().get("id") or resp.json().get("item_id"))

    from app.services.leave_requests import reject_leave_request
    result = run(reject_leave_request(item_id, 20, channel="email_link", reason="not that week"))
    assert "error" not in result, result
    assert switched == []


def test_overtime_and_carryover_never_touch_sharepoint(switched, monkeypatch):
    client = _client()
    resp = client.post("/api/forms/overtime", json={
        "description": "inventory count", "date": date.today().isoformat(), "hours": 4,
        "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code == 200, resp.text
    ot_id = int(resp.json().get("id") or resp.json().get("item_id"))
    from app.services.overtime_requests import approve_overtime_request
    result = run(approve_overtime_request(ot_id, 20, channel="email_link"))
    assert "error" not in result, result

    # Carry-over needs its window open; open it for today.
    from app.services import app_settings
    async def window_open(today=None):
        return True
    monkeypatch.setattr(app_settings, "carryover_payout_open", window_open)
    resp = client.post("/api/forms/carryover-payout", json={
        "type_of_request": "Carry Over", "days": 1, "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code in (200, 400, 422), resp.text     # a refusal is fine; a SharePoint call is not
    assert switched == []


def test_reminders_and_the_outbox_never_touch_sharepoint(switched):
    client = _client()
    start = _future_weekday(30).isoformat()
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": start, "end_date": start,
        "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code == 200, resp.text

    from app.services.request_submission import queue_due_reminders
    from app.tasks.outbox_worker import _drain
    run(queue_due_reminders())
    run(_drain())                                            # every queued message, dropped at the chokepoint
    assert switched == []


# ----- dashboards: every screen's data -----


def test_every_admin_screen_reads_postgres_only(switched):
    client = _client()
    paths = _get_routes("/api/dashboard/admin")
    assert len(paths) >= 15                                  # the sweep really covers the admin screens
    failures, hits = [], []
    for path in paths:
        before = len(switched)
        resp = client.get(path)
        if len(switched) > before:                           # name the screen that reached SharePoint
            hits.append((path, switched[before:]))
        if resp.status_code >= 500 or resp.status_code in (401, 403):
            failures.append((path, resp.status_code, resp.text[:200]))
    assert hits == []
    assert failures == []


def test_the_employee_and_manager_dashboards_read_postgres_only(switched):
    failures = []
    for role, uid, prefix in (("employee", "10", "/api/dashboard/me"), ("manager", "20", "/api/dashboard/team")):
        client = _client(role, uid)
        paths = _get_routes(prefix)
        assert paths                                         # each dashboard has screens to sweep
        for path in paths:
            resp = client.get(path)
            if resp.status_code >= 500 or resp.status_code in (401, 403):
                failures.append((path, resp.status_code, resp.text[:200]))
    assert switched == [], switched
    assert failures == []


# ----- staff and holidays -----


def test_staff_edits_and_a_new_hire_never_touch_sharepoint(switched):
    from app.services.employee_creation import create_employee
    from app.services.manager_assignments import update_employee_managers
    run(update_employee_managers(10, [20]))
    created = run(create_employee({
        "title": "New Hire", "email_address": "newhire@ucsh.com", "location": "Toronto Warden",
        "department": "Sales", "salary_hourly": "Salary",
        "vacation_entitlement": 15, "sick_entitlement": 5,
    }, [20]))
    assert created.get("id")
    assert switched == []


def test_holidays_are_served_and_edited_in_postgres(switched):
    from app.repositories.factory import get_holiday_repository
    repo = get_holiday_repository()
    made = run(repo.create({"Title": "Founders Day", "Date": "2026-12-28", "Province": "Ontario"}))
    assert any(h["id"] == made["id"] for h in run(repo.get_all()))
    run(repo.update_fields(made["id"], {"Title": "Founders' Day"}))
    run(repo.delete(made["id"]))
    assert switched == []


# ----- background jobs and webhooks -----


def test_the_carryover_reset_never_touches_sharepoint(switched, monkeypatch):
    from app.tasks import carryover_reset
    async def not_yet(_year):
        return False
    monkeypatch.setattr(carryover_reset, "_already_ran_this_year", not_yet)
    run(carryover_reset._execute_carryover_reset(date.today().year))
    assert switched == []


def test_the_setup_check_and_held_request_sweeps_never_touch_sharepoint(switched):
    from app.services.employee_validation import validate_all_employee_setups
    run(validate_all_employee_setups())                      # what the admin setup tab runs
    from app.services.held_requests import remind_held_requests, retry_waiting_site
    run(remind_held_requests())                              # the hourly held-request loop's two steps
    run(retry_waiting_site())
    assert switched == []


def test_webhooks_are_ignored_and_never_registered(switched, monkeypatch):
    monkeypatch.setattr(rehearsal, "rehearsal_enabled", lambda: False)   # rehearsal would skip registration on its own
    client = _client()
    resp = client.post("/api/webhooks/sharepoint", json={"value": [{"resource": "x", "clientState": "y"}]})
    assert resp.status_code == 202
    from app.tasks.subscription_manager import register_all_subscriptions
    run(register_all_subscriptions())                        # returns at once after the switch
    assert switched == []


# ----- one request or one person: the screens and actions that take an id -----


def _submit_leave(client: TestClient, days_ahead: int) -> int:
    """Submit one vacation day through the request form and return its number."""
    start = _future_weekday(days_ahead).isoformat()
    resp = client.post("/api/forms/leave", json={
        "leave_type": "Vacation", "start_date": start, "end_date": start,
        "submitter_email": "alice@ucsh.com",
    })
    assert resp.status_code == 200, resp.text
    return int(resp.json().get("id") or resp.json().get("item_id"))


def test_actions_on_one_request_or_person_never_touch_sharepoint(switched):
    from urllib.parse import urlparse
    from app.services.approval_links import generate_approval_url
    admin = _client()
    first = _submit_leave(admin, 35)
    second = _submit_leave(admin, 40)
    third = _submit_leave(admin, 45)
    day = _future_weekday(35).isoformat()

    calls = [
        # One person: profile, setup check, dashboard link, managers, entitlements.
        ("get", "/api/dashboard/admin/employees/10", None),
        ("get", "/api/dashboard/admin/validate-employee/10", None),
        ("get", "/api/dashboard/admin/employee-dashboard-link/10", None),
        ("patch", "/api/dashboard/admin/manager-assignments/10", {"manager_ids": [20]}),
        ("patch", "/api/dashboard/admin/employees/10/entitlements",
         {"changes": {"vacation": 9}, "fields": {}, "reason": "HR correction"}),
        # One request: timeline, edit, reminder, re-ask, approve, reject, delete.
        ("get", f"/api/dashboard/admin/requests/leave/{first}/timeline", None),
        ("post", f"/api/dashboard/admin/edit/leave/{first}",
         {"Days": 1, "LeaveType": "Vacation", "StartDate": day, "EndDate": day, "reason": "typo"}),
        ("post", f"/api/dashboard/admin/send-reminder/leave/{first}", None),
        ("post", f"/api/dashboard/admin/reprocess/leave/{first}", {"reason": "resend"}),
        ("post", f"/api/dashboard/admin/approve/leave/{first}", None),
        ("post", f"/api/dashboard/admin/refund/leave/{first}", None),
        ("post", f"/api/dashboard/admin/reject/leave/{second}", {"reason": "not needed"}),  # reject now requires a reason (#433)
        ("post", f"/api/dashboard/admin/requests/leave/{third}/delete", {"reason": "duplicate", "refund": False}),
        # Health and status pages.
        ("get", "/health", None),
        ("get", "/status", None),
    ]
    failures, hits = [], []
    for method, path, body in calls:
        before = len(switched)
        resp = getattr(admin, method)(path, **({"json": body} if body is not None else {}))
        if len(switched) > before:
            hits.append((path, switched[before:]))
        if resp.status_code >= 500:
            failures.append((path, resp.status_code, resp.text[:200]))

    # The employee's and the manager's own views of a request, and a manager decision.
    fourth = _submit_leave(admin, 50)
    for role, uid, method, path in (
        ("employee", "10", "get", f"/api/dashboard/me/requests/leave/{fourth}/timeline"),
        ("manager", "20", "get", f"/api/dashboard/team/requests/leave/{fourth}/timeline"),
        ("manager", "20", "post", f"/api/dashboard/team/approve/leave/{fourth}"),
    ):
        before = len(switched)
        resp = getattr(_client(role, uid), method)(path)
        if len(switched) > before:
            hits.append((path, switched[before:]))
        if resp.status_code >= 500:
            failures.append((path, resp.status_code, resp.text[:200]))

    # The approval email link: the confirm page, then the decision.
    fifth = _submit_leave(admin, 55)
    version = run(_record("leave", fifth)).approval_version
    url = urlparse(generate_approval_url("leave", fifth, "approve", 20, approval_version=version))
    link = f"{url.path}?{url.query}"
    for method in ("get", "post"):
        before = len(switched)
        resp = getattr(admin, method)(link)
        if len(switched) > before:
            hits.append((link, switched[before:]))
        if resp.status_code >= 500:
            failures.append((link, resp.status_code, resp.text[:200]))

    assert hits == []
    assert failures == []


def test_an_sms_reply_never_touches_sharepoint(switched, monkeypatch):
    from app.routes import twilio as twilio_route
    if hasattr(twilio_route, "_validate_twilio_signature"):
        monkeypatch.setattr(twilio_route, "_validate_twilio_signature", lambda *a, **k: True)
    admin = _client()
    _submit_leave(admin, 60)
    resp = admin.post("/api/twilio/sms", data={"From": "+16475551234", "Body": "YES"})
    assert resp.status_code < 500, resp.text
    assert switched == []


def test_a_new_request_is_pending_for_admin_and_manager_and_not_stuck(switched):
    # After the switch a request names its manager by staff id (ManagerID), not a
    # SharePoint lookup id; it must still count as fully processed everywhere.
    admin = _client()
    item_id = _submit_leave(admin, 65)

    pending = admin.get("/api/dashboard/admin/pending").json().get("pending", [])
    assert any(str(p.get("id")) == str(item_id) for p in pending), pending

    stuck = admin.get("/api/dashboard/admin/stuck-requests").json().get("stuck", [])
    assert not any(str(s.get("id")) == str(item_id) for s in stuck), stuck

    team = _client("manager", "20").get("/api/dashboard/team/pending").json().get("pending", [])
    assert any(str(p.get("id")) == str(item_id) for p in team), team

    mine = _client("employee", "10").get("/api/dashboard/me/requests").json()
    mine = mine.get("requests", mine) if isinstance(mine, dict) else mine
    assert any(str(r.get("id")) == str(item_id) for r in mine), mine   # the person sees their own request

    record = run(_record("leave", item_id))
    assert record.employee_name == "Alice Salary"           # named from the staff table, not SharePoint

    # Reprocessing a healthy request must not report a missing manager lookup:
    # on Postgres the manager is the staff id, not a SharePoint lookup id.
    redo = admin.post(f"/api/dashboard/admin/reprocess/leave/{item_id}", json={"reason": "resend"})
    assert redo.status_code == 200, redo.text
    assert "missing_manager_lookup" not in redo.json().get("remaining_issues", []), redo.json()
    assert switched == []


# ----- identifiers and columns the Postgres rows carry -----


def test_admin_stats_and_pending_count_postgres_rows_in_the_database(switched):
    # After the switch the stats are counted by the database and the pending
    # queues read only pending rows; both must still agree with what was submitted.
    from app.services.leave_requests import approve_leave_request
    admin = _client()
    approved = _submit_leave(admin, 70)
    waiting = _submit_leave(admin, 71)
    assert "error" not in run(approve_leave_request(approved, 20, channel="email_link"))

    stats = admin.get("/api/dashboard/admin/stats").json()
    by_status = stats["leave_by_status"]
    assert by_status.get("Approved", 0) >= 1 and by_status.get("Pending", 0) >= 1
    assert stats["total_requests"]["leave"] == sum(by_status.values())   # totals add up

    pending = admin.get("/api/dashboard/admin/pending").json()["pending"]
    ids = {str(p["id"]) for p in pending}
    assert str(waiting) in ids and str(approved) not in ids   # only the one still waiting
    assert all(p.get("Status") == "Pending" for p in pending)
    assert switched == []


def test_large_responses_are_compressed(switched):
    # The admin requests list runs to megabytes of JSON; it must go out gzipped.
    admin = _client()
    for days in (72, 73, 74):
        _submit_leave(admin, days)
    resp = admin.get("/api/dashboard/admin/requests", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers.get("content-encoding") == "gzip"


def test_an_approval_and_its_refund_write_the_balance_log_the_screens_read(switched):
    # The admin "What it did to balances" panel and the Audit trail read
    # BalanceAuditLog; the Postgres decision path must keep writing it.
    import json
    from app.services.leave_requests import approve_leave_request, refund_leave_request
    item_id = _submit_leave(_client(), 40)
    assert "error" not in run(approve_leave_request(item_id, 20, channel="email_link"))
    log = json.loads(run(_record("leave", item_id)).extra["BalanceAuditLog"])
    assert [entry["action"] for entry in log] == ["approve"]
    step = log[0]["steps"][0]
    assert step["before"] == {"CurrentOvertimeBalance": 2.0}  # one vacation day spends make-up first
    assert step["after"] == {"CurrentOvertimeBalance": 1.0}

    assert "error" not in run(refund_leave_request(item_id, "admin", channel="admin_dashboard"))
    log = json.loads(run(_record("leave", item_id)).extra["BalanceAuditLog"])
    assert [entry["action"] for entry in log] == ["approve", "refund"]
    assert log[1]["steps"][0]["after"] == {"CurrentOvertimeBalance": 2.0}   # put back
    assert switched == []


def test_a_carry_over_approval_records_its_new_balance(switched):
    # The requests table and the export show "New balance" for a carry-over.
    from app.services import request_decisions

    async def seed():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="carryover_payout", item_id=7001, status="pending", employee_id="10",
                employee_email="alice@ucsh.com", days=2.0, request_option="carry_over",
            ))
            await session.commit()
    run(seed())
    result = run(request_decisions.approve("carryover_payout", 7001, "20", "admin_dashboard"))
    assert result.get("status") == "approved", result
    extra = run(_record("carryover_payout", 7001)).extra
    assert extra["NewBalance"] == "{Vacation:8.0, CarryOver:2.0, Payout:0.0}"
    assert extra["SystemState"] == "Processed"
    assert switched == []


def test_a_request_is_named_by_its_staff_id_when_it_has_no_email():
    # After the switch the SharePoint user ids no longer resolve, so EmployeeID
    # (and ManagerID for the manager column) must name the person.
    from app.routes.dashboard import _PersonNames, _resolve_sp_user_name
    names = _PersonNames()
    names.by_staff_id.update({"10": "Alice Salary", "20": "Boss Person"})
    row = {"EmployeeID": "10", "ManagerID": "20", "SubmittedTestLookupId": 99}
    assert _resolve_sp_user_name(row, "SubmittedTest", names) == "Alice Salary"
    assert _resolve_sp_user_name(row, "SubmittedBy", names) == "Alice Salary"
    assert _resolve_sp_user_name(row, "Manager", names) == "Boss Person"
    assert _resolve_sp_user_name({"EmployeeID": "77"}, "SubmittedTest", names) == ""


def test_an_overlap_is_found_by_staff_id_when_there_is_no_email():
    from app.services.overlap_detection import find_conflict_for_row
    day = _future_weekday(10).isoformat()
    approved = {"id": "1", "fields": {"Status": "Approved", "EmployeeID": "10",
                                      "StartDate": day, "EndDate": day, "Days": 1}}
    pending = {"id": "2", "fields": {"Status": "Pending", "EmployeeID": "10",
                                     "StartDate": day, "EndDate": day, "Days": 1}}
    other = {"id": "3", "fields": {"Status": "Approved", "EmployeeID": "11",
                                   "StartDate": day, "EndDate": day, "Days": 1}}
    conflict = find_conflict_for_row([approved, pending, other], pending)
    assert conflict and str(conflict["item_id"]) == "1"      # the same person's approved day
    # A placeholder id from an unmatched import names nobody, so it never matches.
    ghost_a = {"id": "4", "fields": {"Status": "Approved", "EmployeeID": "spuser:5",
                                     "StartDate": day, "EndDate": day, "Days": 1}}
    ghost_b = {"id": "5", "fields": {"Status": "Pending", "EmployeeID": "spuser:5",
                                     "StartDate": day, "EndDate": day, "Days": 1}}
    assert find_conflict_for_row([ghost_a, ghost_b], ghost_b) is None


def test_old_carry_over_findings_are_kept_but_not_listed(switched):
    # The pre-app carry-overs with no balance log stay in the table, hidden from
    # the legacy data screen and its counts; a newer one still shows.
    from datetime import datetime, timezone
    from app.services import legacy_findings

    async def seed():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="carryover_payout", item_id=8001, status="approved", employee_id="10",
                decided_at=datetime(2025, 4, 1, tzinfo=timezone.utc), days=1.0, request_option="carry_over",
            ))
            session.add(RequestRecord(
                kind="carryover_payout", item_id=8002, status="approved", employee_id="10",
                decided_at=datetime(2026, 5, 1, tzinfo=timezone.utc), days=1.0, request_option="carry_over",
            ))
            for item_id in (8001, 8002):
                await legacy_findings.record_finding(
                    session, "approved_no_balance_change", "request", "carryover_payout", item_id,
                    "The request was approved but no balance change was recorded.",
                    {"has_audit_log": False}, None,
                )
            await session.commit()
    run(seed())
    listing = run(legacy_findings.list_findings(case="approved_no_balance_change"))
    assert [f["subject_id"] for f in listing["findings"]] == ["8002"]
    assert listing["total"] == 1
    assert run(legacy_findings.counts_by_case())["approved_no_balance_change"] == 1

    async def stored():
        async with async_session() as session:
            return (await session.execute(select(LegacyFinding).where(
                LegacyFinding.case == "approved_no_balance_change"))).scalars().all()
    assert len(run(stored())) == 2                          # nothing deleted


def test_the_switch_points_manager_links_at_staff_ids(monkeypatch):
    # Imported links carry Microsoft 365 ids; after the flip they carry the staff
    # id of the person they name, the id every manager picker uses.
    from app.models.staff import Staff, StaffSupervisor
    from app.services import storage_switch

    async def seed():
        async with async_session() as session:
            boss = Staff(sp_item_id="20", name="Boss Person")
            alice = Staff(sp_item_id="10", name="Alice Salary", title_link_sp_user_id=345)
            session.add_all([boss, alice])
            await session.flush()
            session.add_all([
                StaffSupervisor(staff_id=alice.id, relation="manager",
                                manager_sp_user_id=612, manager_name="Boss Person"),
                StaffSupervisor(staff_id=alice.id, relation="manager",
                                manager_sp_user_id=10, manager_name="Gone Away"),
            ])
            await session.commit()

    async def links():
        async with async_session() as session:
            rows = (await session.execute(select(StaffSupervisor).order_by(StaffSupervisor.id))).scalars().all()
            alice = (await session.execute(select(Staff).where(Staff.sp_item_id == "10"))).scalar_one()
            return [(r.manager_name, r.manager_sp_user_id) for r in rows], alice.title_link_sp_user_id

    run(wipe_staff())
    run(_wipe())
    try:
        run(seed())
        run(storage_switch._flip_settings_to_postgres())
        pairs, own_link = run(links())
        assert pairs == [("Boss Person", 20), ("Gone Away", None)]   # 10 would have been Alice's staff id
        assert own_link == 10
    finally:
        run(_wipe())
        run(wipe_staff())


def test_managers_supervisors_and_teams_hold_after_the_switch(switched):
    # The assignment screen, the picker, the supervisor link, the team view and
    # approval routing all agree on Boss after the switch converts his id.
    from app.models.staff import Staff, StaffSupervisor
    admin = _client()
    alice = next(a for a in admin.get("/api/dashboard/admin/manager-assignments").json()["assignments"]
                 if a["name"] == "Alice Salary")
    assert alice["managers"] == [{"sp_user_id": 20, "name": "Boss Person"}]   # 612 became Boss's staff id
    users = {u["name"]: u["sp_user_id"] for u in admin.get("/api/dashboard/admin/sp-users").json()["users"]}
    assert users["Boss Person"] == 20                         # the picker offers the same id

    async def supervisor_link():
        async with async_session() as session:
            owner = (await session.execute(select(Staff).where(Staff.sp_item_id == "10"))).scalar_one()
            link = (await session.execute(select(StaffSupervisor).where(
                StaffSupervisor.staff_id == owner.id, StaffSupervisor.relation == "supervisor_link",
            ))).scalar_one()
            return link.manager_sp_user_id
    assert run(supervisor_link()) == 20                       # matched by the Supervisor text

    # Saving the managers back keeps Boss and his name (no blank, no stranger).
    saved = admin.patch("/api/dashboard/admin/manager-assignments/10", json={"manager_ids": [20]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["managers"] == [{"sp_user_id": 20, "name": "Boss Person"}]

    members = _client("manager", "20").get("/api/dashboard/team/members").json()["members"]
    assert [m["name"] for m in members] == ["Alice Salary"]   # Boss's team is Alice

    # A new request routes to Boss: his approval email and text are queued.
    item_id = _submit_leave(admin, 45)

    async def approvals():
        async with async_session() as session:
            rows = (await session.execute(select(Outbox).where(Outbox.message_type == "manager_approval"))).scalars().all()
            return sorted((row.channel, row.recipient) for row in rows)
    assert run(approvals()) == [("email", "boss@ucsh.com"), ("sms", "+16475551234")]
    assert run(_record("leave", item_id)).status == "pending"
    assert switched == []


def test_the_switch_queues_no_message_and_reports_what_landed(monkeypatch):
    # The whole switch, start to finish, on a pending request already imported:
    # nothing is queued to send, and the summary line counts what landed.
    from app.services import storage_switch
    from app.services.shadow_sync import SYNC_STEPS
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "sharepoint")
    monkeypatch.setattr(storage_switch, "PAUSE_SETTLE_SECONDS", 0)
    run(_wipe())
    run(wipe_staff())

    async def seed():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="leave", item_id=9001, sp_item_id="9001", status="pending", employee_id="10",
                employee_email="alice@ucsh.com", leave_type="Vacation", days=1.0,
                start_date=_future_weekday(15), end_date=_future_weekday(15),
            ))
            await session.commit()
    run(seed())

    async def no_step(session, sync_run):
        return {}                                             # the copy itself is covered elsewhere
    monkeypatch.setattr("app.services.storage_switch.SYNC_STEPS", [no_step])
    try:
        result = run(storage_switch.flip_to_postgres())
        summary = storage_switch.switch_status()["summary"]
        assert result["storage"] == POSTGRES
        assert summary["messages_queued_by_switch"] == 0
        assert summary["pending"] == {"leave": 1}

        # The first reminder pass after the switch leaves the imported request alone:
        # the old path never emailed its manager from this database.
        from app.services.request_submission import queue_due_reminders
        assert run(queue_due_reminders()) == 0

        async def outbox_rows():
            async with async_session() as session:
                return (await session.execute(select(Outbox))).scalars().all()
        assert run(outbox_rows()) == []
    finally:
        assert SYNC_STEPS                                     # the real steps were untouched
        run(_wipe())
        run(wipe_staff())


def test_a_request_on_a_shared_mailbox_is_named_by_its_staff_id():
    # Two records share ap@ucsh.com: the staff id on the row names the person,
    # not whichever record the email happens to map to.
    from app.routes.dashboard import _PersonNames, _resolve_sp_user_name
    names = _PersonNames()
    names.by_email["ap@ucsh.com"] = "Accounts Payable"
    names.by_staff_id.update({"40": "Accounts Payable", "41": "Kim Clerk"})
    row = {"EmployeeID": 41.0, "SubmitterEmail": "ap@ucsh.com"}
    assert _resolve_sp_user_name(row, "SubmittedTest", names) == "Kim Clerk"
    assert _resolve_sp_user_name({"SubmitterEmail": "ap@ucsh.com"}, "SubmittedTest", names) == "Accounts Payable"
