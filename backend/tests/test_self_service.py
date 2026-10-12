"""A signed-in employee submits a request from the request page (#143).

The submitter comes only from the signed token, the form is checked before
anything reaches SharePoint, and the request goes through the same creation
functions as the Microsoft Form path, labelled as coming from the page.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import self_service
from app.services import request_intake
from app.services.dashboard_tokens import generate_dashboard_token

PAT = {"id": "7", "fields": {"Title": "Pat Q Staff", "EmailAddress": "pat@ucsh.com"}}


@pytest.fixture
def client(monkeypatch):
    """The self-service router alone, with SharePoint stubbed.

    Returns:
        A TestClient with ``.created`` (calls to the creation functions) and
        ``.linked`` (whether Pat is in the site's user list) attached.
    """
    app = FastAPI()
    app.include_router(self_service.router, prefix="/api/dashboard")
    test_client = TestClient(app)
    test_client.created = []
    test_client.linked = True
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)

    async def by_id(item_id):
        return PAT if str(item_id) == "7" else None

    async def lookup(email):
        return 12 if test_client.linked else None

    def recorder(kind):
        async def create(form_data, email, source=None):
            test_client.created.append((kind, form_data, email, source))
            return {"id": "500"}
        return create

    monkeypatch.setattr(self_service, "get_employee_by_id", by_id)
    monkeypatch.setattr(self_service, "_resolve_user_lookup_id", lookup)
    monkeypatch.setattr(request_intake, "process_new_leave_request", recorder("leave"))
    monkeypatch.setattr(request_intake, "process_new_overtime_request", recorder("overtime"))
    monkeypatch.setattr(request_intake, "process_new_carryover_payout", recorder("carryover-payout"))
    return test_client


def _submit(client, request_type, body, uid="7"):
    """POST a form with a valid employee token for ``uid``."""
    token = generate_dashboard_token("employee", uid)
    return client.post(f"/api/dashboard/me/requests/{request_type}", params=token, json=body)


VACATION = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-04", "notes": "Family trip"}


def test_a_vacation_request_is_created_for_the_signed_in_employee(client):
    response = _submit(client, "leave", VACATION)

    assert response.status_code == 200
    assert response.json() == {"status": "submitted", "request_type": "leave", "item_id": "500"}
    kind, form_data, email, source = client.created[0]
    assert (kind, email, source) == ("leave", "pat@ucsh.com", "Request page")
    assert form_data["start_date"] == "2026-11-02" and form_data["end_date"] == "2026-11-04"
    assert (form_data["first_name"], form_data["last_name"]) == ("Pat", "Q Staff")


def test_a_partial_day_is_one_date_measured_in_hours(client):
    body = {"leave_type": "Half Day or Partial Day Off", "start_date": "2026-11-02", "partial_hours": 3.5}
    assert _submit(client, "leave", body).status_code == 200
    form_data = client.created[0][1]
    assert form_data["end_date"] == "2026-11-02" and form_data["partial_hours"] == 3.5


@pytest.mark.parametrize("body, message", [
    ({**VACATION, "leave_type": "Holiday"}, "listed leave types"),
    ({**VACATION, "end_date": "2026-11-01"}, "cannot be before"),
    ({"leave_type": "Vacation", "start_date": "2026-11-02"}, "last day"),
    ({"leave_type": "Half Day or Partial Day Off", "start_date": "2026-11-02", "partial_hours": 8}, "less than 8"),
    ({**VACATION, "start_date": "not a date"}, "Start date is not valid"),
])
def test_a_bad_leave_form_is_refused_with_a_reason(client, body, message):
    response = _submit(client, "leave", body)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert client.created == []


def test_overtime_must_be_in_half_hours(client):
    bad = _submit(client, "overtime", {"description": "Inventory", "date": "2026-11-02", "hours": 1.25})
    good = _submit(client, "overtime", {"description": "Inventory", "date": "2026-11-02", "hours": 1.5})
    assert bad.status_code == 400 and "half-hour" in bad.json()["detail"]
    assert good.status_code == 200 and client.created[0][0] == "overtime"


def test_carryover_takes_only_the_two_choices(client):
    assert _submit(client, "carryover-payout", {"type_of_request": "Cash", "days": 1}).status_code == 400
    assert _submit(client, "carryover-payout", {"type_of_request": "Payout", "days": 2}).status_code == 200


def test_an_unknown_request_type_is_refused(client):
    assert _submit(client, "lunch", {}).status_code == 400


def test_no_token_no_request(client):
    response = client.post("/api/dashboard/me/requests/leave", json=VACATION)
    assert response.status_code == 422                         # token query parameters are required
    assert client.created == []


def test_a_forged_token_is_refused(client):
    token = generate_dashboard_token("employee", "7")
    response = client.post("/api/dashboard/me/requests/leave",
                           params={**token, "uid": "8"}, json=VACATION)   # someone else's id
    assert response.status_code == 401


def test_nothing_is_created_while_processing_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    assert _submit(client, "leave", VACATION).status_code == 503


def test_without_the_email_column_an_unlinked_employee_is_told_why(client, monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", False)
    client.linked = False
    response = _submit(client, "leave", VACATION)
    assert response.status_code == 409 and "not linked" in response.json()["detail"]
    assert client.created == []


def test_with_requests_in_postgres_an_unlinked_employee_can_submit(client, monkeypatch):
    # The "not on the SharePoint site yet" case (#196): no columns, no site visit, still sent.
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", False)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    client.linked = False
    assert _submit(client, "leave", VACATION).status_code == 200
    assert len(client.created) == 1


def test_with_the_email_column_an_unlinked_employee_can_submit(client):
    client.linked = False
    assert _submit(client, "leave", VACATION).status_code == 200


def test_a_sharepoint_failure_is_reported(client, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("Graph 500")

    monkeypatch.setattr(request_intake, "process_new_leave_request", broken)
    assert _submit(client, "leave", VACATION).status_code == 502
