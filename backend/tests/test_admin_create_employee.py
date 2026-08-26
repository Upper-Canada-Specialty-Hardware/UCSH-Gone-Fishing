"""The admin dashboard's add-employee endpoint.

The creation itself is covered by test_employee_creation. These cover only what
the endpoint adds on top: it is reachable without a token (the admin dashboard
is unauthenticated by design), it hands the chosen supervisors through as
integers, and it turns a validation failure into a readable 400 rather than a
500.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes.dashboard import router as dashboard_router
from app.services import employee_creation as ec

GOOD = {
    "title": "New Hire",
    "email_address": "newhire@ucsh.com",
    "location": "Toronto Warden",
    "department": "Warehouse",
    "salary_hourly": "Salary",
    "vacation_entitlement": 15,
    "sick_entitlement": 5,
    "manager_ids": [501],
}


@pytest.fixture
def client(monkeypatch):
    # The endpoint is gated on PROCESSING_ENABLED, which defaults off.
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    app = FastAPI()
    app.include_router(dashboard_router)
    # raise_server_exceptions=False so an unhandled error surfaces as a 500
    # response, the way a caller would see it, rather than re-raising.
    return TestClient(app, raise_server_exceptions=False)


def _capture(monkeypatch):
    """Replace the creation service with a spy, returning what it received."""
    seen = {}

    async def _fake(form_data, manager_ids):
        seen["form"] = form_data
        seen["ids"] = manager_ids
        return {"id": "777", "fields": {"Title": form_data.get("title")}}

    monkeypatch.setattr(ec, "create_employee", _fake)
    return seen


def test_it_needs_no_token_and_passes_the_supervisors_through(client, monkeypatch):
    seen = _capture(monkeypatch)

    resp = client.post("/admin/employees", json=GOOD)

    assert resp.status_code == 200          # no token supplied, still accepted
    assert seen["ids"] == [501]
    assert seen["form"]["title"] == "New Hire"


def test_supervisor_ids_are_coerced_to_ints(client, monkeypatch):
    # The picker may hand back strings; a stray unparseable value is dropped
    # rather than breaking the Person-field write.
    seen = _capture(monkeypatch)

    client.post("/admin/employees", json={**GOOD, "manager_ids": ["501", "not-an-id", 502]})

    assert seen["ids"] == [501, 502]


def test_a_validation_failure_becomes_a_readable_400(client, monkeypatch):
    async def _raise(form_data, manager_ids):
        raise ec.EmployeeValidationError("At least one supervisor must be assigned.")

    monkeypatch.setattr(ec, "create_employee", _raise)

    resp = client.post("/admin/employees", json={**GOOD, "manager_ids": []})

    assert resp.status_code == 400
    assert "supervisor" in resp.json()["detail"]


def test_processing_disabled_is_a_503(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    _capture(monkeypatch)

    resp = client.post("/admin/employees", json=GOOD)

    assert resp.status_code == 503
