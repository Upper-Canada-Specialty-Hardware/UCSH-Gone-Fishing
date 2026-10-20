"""Two dashboard routes change behaviour once staff/requests live in Postgres.

- /team/employees: the new hire's supervisor is the signed-in manager. On
  Postgres that is their own staff id, with no SharePoint user-list resolve
  (finding 7).
- /admin/reprocess/leave: a manual re-ask goes through the outbox (queue_reminder)
  rather than the direct send or the dispatcher (finding 4).

Both are driven through a TestClient so the real route body runs; only the
services the route calls are stubbed.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import dashboard
from app.routes.dashboard import DashboardUser, _PersonNames, get_dashboard_user, router as dashboard_router
from app.services import employee_creation as ec
from app.services import leave_requests as lr
from app.services import request_submission as rsub
from app.tasks import dispatcher


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    app = FastAPI()
    app.include_router(dashboard_router)
    return app, TestClient(app, raise_server_exceptions=False)


# ----- /team/employees: the manager is the supervisor, by staff id (finding 7) -----


def test_team_create_uses_the_manager_staff_id_on_postgres(app_client, monkeypatch):
    app, client = app_client
    app.dependency_overrides[get_dashboard_user] = lambda: DashboardUser(role="manager", user_id="42")
    seen = {}

    async def fake_by_id(uid):
        return {"id": str(uid), "fields": {"Title": "Steve Faith", "EmailAddress": "stevef@ucsh.com"}}

    async def in_pg():
        return True

    async def boom(_email):
        raise AssertionError("the SharePoint user list must not be resolved on Postgres")

    async def fake_create(form_data, supervisor_ids):
        seen["ids"] = supervisor_ids
        seen["form"] = form_data
        return {"id": "900", "fields": {"Title": form_data.get("title")}}

    async def fake_after(record):
        return record

    monkeypatch.setattr(dashboard, "get_employee_by_id", fake_by_id)
    monkeypatch.setattr(dashboard, "staff_in_postgres", in_pg)
    monkeypatch.setattr(dashboard, "_resolve_user_lookup_id", boom)
    monkeypatch.setattr(dashboard, "_after_employee_created", fake_after)
    monkeypatch.setattr(ec, "create_employee", fake_create)

    body = {"title": "New Hire", "email_address": "newhire@ucsh.com", "location": "British Columbia",
            "department": "Warehouse", "salary_hourly": "Salary",
            "vacation_entitlement": 15, "sick_entitlement": 5}
    resp = client.post("/team/employees", json=body)

    assert resp.status_code == 200, resp.text
    assert seen["ids"] == [42]                                  # the manager's own staff id, as an int
    app.dependency_overrides.clear()


# ----- /admin/reprocess/leave: re-ask through the outbox on Postgres (finding 4) -----


def test_admin_reprocess_leave_queues_through_the_outbox_on_postgres(app_client, monkeypatch):
    _app, client = app_client
    calls = []

    async def get_item(list_id, item_id):
        return {"id": str(item_id), "fields": {"Status": "Pending", "LeaveType": "Vacation"}}

    async def in_pg():
        return True

    async def fake_queue(kind, request_id):
        calls.append((kind, str(request_id)))
        return True

    async def never_send(*_a, **_k):
        raise AssertionError("the direct SharePoint send must not run on Postgres")

    async def fake_lookups():
        return {}, {}, _PersonNames(), {}

    async def fake_emailed(_entries):
        return None

    monkeypatch.setattr(dashboard.request_store, "get_list_item", get_item)
    monkeypatch.setattr(dashboard, "requests_in_postgres", in_pg)
    monkeypatch.setattr(rsub, "queue_reminder", fake_queue)
    monkeypatch.setattr(lr, "send_approval_email", never_send)
    monkeypatch.setattr(dispatcher, "dispatch_change", never_send)
    monkeypatch.setattr(dashboard, "_build_staff_lookups", fake_lookups)
    monkeypatch.setattr(dashboard, "fetch_approval_email_records", fake_emailed)
    monkeypatch.setattr(dashboard, "_diagnose_stuck_leave", lambda *a, **k: ([], "ok"))

    resp = client.post("/admin/reprocess/leave/123", json={"reason": "please resend"})

    assert resp.status_code == 200, resp.text
    assert calls == [("leave", "123")]                          # queued, not sent directly
