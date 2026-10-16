"""A manager logging a sick day for a team member must be gated and safe.

The feature exists because staff who call in sick are away from a computer and
the manager ends up chasing them to log it. The risk in letting a manager write
an approved absence for someone else is twofold: it must only be possible where
HR has enabled it (by the manager's own location) and only for the manager's
own team, and it must never leave a half-made request behind when refused.

The province gate and the field assembly are pure and tested directly. The
async wrapper's SharePoint touchpoints are faked so nothing here reaches Graph;
what is pinned is the order, every refusal happens before the create, and
that the approval goes through the shared handler rather than a private path.
The route tests cover only what the endpoint adds: the token, the status codes,
and the flag the dashboard reads to decide whether to show the button.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import dashboard as dashboard_route
from app.routes.dashboard import router as dashboard_router
from app.services import sick_day_on_behalf as sd
from app.services.dashboard_tokens import generate_dashboard_token
from app.services.sick_day_on_behalf import (
    SickDayNotPermitted,
    SickDayOnBehalfError,
    build_sick_day_fields,
    is_assigned_to,
    log_sick_day_on_behalf,
    manager_may_log_sick_days,
)

BC_MANAGER = {
    "id": "42",
    "fields": {"Title": "Steve Faith", "EmailAddress": "stevef@ucsh.com", "Location": "British Columbia"},
}
ON_MANAGER = {
    "id": "43",
    "fields": {"Title": "Toronto Boss", "EmailAddress": "boss@ucsh.com", "Location": "Toronto Warden"},
}
EMPLOYEE = {
    "id": "480",
    "fields": {
        "Title": "Roland Alvarez",
        "EmailAddress": "rolanda@ucsh.com",
        "Location": "British Columbia",
        "Department": "Warehouse",
        "AllManagers": [{"LookupId": 501, "LookupValue": "Steve Faith"}],
        "CurrentSickDayBalance": 5,
    },
}
# A Monday-to-Tuesday range: two working days, no weekend to reason about.
FORM = {"employee_id": "480", "start_date": "2026-09-07", "end_date": "2026-09-08", "notes": "flu"}


# ----- the province gate -----

def test_a_bc_manager_passes_the_gate():
    assert manager_may_log_sick_days(BC_MANAGER["fields"]) is True


def test_an_ontario_manager_is_outside_the_gate_by_default():
    assert manager_may_log_sick_days(ON_MANAGER["fields"]) is False


@pytest.mark.parametrize("location", ["", None, "Mars"])
def test_a_blank_or_unmapped_location_is_refused_not_an_error(location):
    # The gate only decides whether a button shows; it must never raise.
    assert manager_may_log_sick_days({"Location": location}) is False


def test_ontario_can_be_enabled_by_the_setting_alone(monkeypatch):
    # Turning Ontario on is a Railway variable change, not a code change, and
    # the setting tolerates casual formatting.
    monkeypatch.setattr(settings, "SICK_DAY_ON_BEHALF_PROVINCES", " bc, on ")
    assert manager_may_log_sick_days(ON_MANAGER["fields"]) is True
    assert manager_may_log_sick_days({"Location": "Ottawa"}) is True


def test_a_blank_setting_disables_it_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "SICK_DAY_ON_BEHALF_PROVINCES", "")
    assert manager_may_log_sick_days(BC_MANAGER["fields"]) is False


# ----- the assignment gate -----

def test_assignment_matches_the_all_managers_display_name():
    assert is_assigned_to(EMPLOYEE["fields"], "Steve Faith") is True
    assert is_assigned_to(EMPLOYEE["fields"], "Toronto Boss") is False


def test_no_supervisors_means_not_assigned():
    assert is_assigned_to({"AllManagers": None}, "Steve Faith") is False
    assert is_assigned_to({}, "Steve Faith") is False


# ----- the pure field assembly -----

def test_the_fields_are_a_pending_sick_request_in_the_employees_name():
    fields = build_sick_day_fields(FORM, "Roland Alvarez", "Steve Faith")

    assert fields["LeaveType"] == "Sick or Personal Day"
    assert fields["Status"] == "Pending"                    # what approve_leave_request requires
    assert fields["ApproveProcessedFlag"] == "Not Processed"
    assert fields["StartDate"] == "2026-09-07"
    assert fields["EndDate"] == "2026-09-08"
    # Compound Title, as every leave request: name, separator, description.
    assert fields["Title"] == "Roland Alvarez /// Logged by Steve Faith: flu"


def test_provenance_is_recorded_even_without_notes():
    fields = build_sick_day_fields({**FORM, "notes": ""}, "Roland Alvarez", "Steve Faith")
    assert fields["Title"] == "Roland Alvarez /// Logged by Steve Faith"


def test_a_missing_end_date_means_a_single_day():
    fields = build_sick_day_fields({"start_date": "2026-09-07"}, "R", "S")
    assert fields["EndDate"] == "2026-09-07"


def test_a_missing_start_date_is_refused():
    with pytest.raises(SickDayOnBehalfError, match="start date"):
        build_sick_day_fields({}, "R", "S")


def test_an_unreadable_date_is_refused():
    with pytest.raises(SickDayOnBehalfError):
        build_sick_day_fields({"start_date": "next monday"}, "R", "S")


def test_an_end_before_the_start_is_refused():
    with pytest.raises(SickDayOnBehalfError, match="on or after"):
        build_sick_day_fields({"start_date": "2026-09-08", "end_date": "2026-09-07"}, "R", "S")


# ----- the async wrapper -----

def _patch(monkeypatch, *, manager=BC_MANAGER, employee=EMPLOYEE, conflict=None,
           approve_result=None, employee_resolves=True):
    """Fake every SharePoint touchpoint and capture the writes, in order."""
    calls = {"created": None, "stamp": None, "approved": None, "order": []}
    directory = {manager["id"]: manager, employee["id"]: employee}

    async def _get_by_id(item_id):
        return directory.get(str(item_id))

    async def _resolve(email):
        if email == employee["fields"]["EmailAddress"]:
            return 601 if employee_resolves else None
        return 501  # the manager

    async def _holidays(province):
        return []

    async def _overlap(**kwargs):
        calls["overlap"] = kwargs
        return conflict

    async def _create(list_id, fields):
        calls["created"] = fields
        calls["order"].append("create")
        return {"id": "777", "fields": fields}

    async def _update(list_id, item_id, fields):
        calls["stamp"] = (str(item_id), fields)
        calls["order"].append("stamp")
        return {"id": str(item_id), "fields": fields}

    # The sick-day path now passes channel="manager_logs_sick_day"; capture it.
    async def _approve(request_id, manager_id, *args, **kwargs):
        calls["approved"] = (str(request_id), str(manager_id))
        calls["order"].append("approve")
        return approve_result or {"status": "approved", "balances": {"CurrentSickDayBalance": 3}}

    monkeypatch.setattr(sd, "get_employee_by_id", _get_by_id)
    monkeypatch.setattr(sd, "_resolve_user_lookup_id", _resolve)
    monkeypatch.setattr(sd, "get_holidays_for_province", _holidays)
    monkeypatch.setattr(sd, "check_leave_overlap", _overlap)
    monkeypatch.setattr(sd.request_store, "create_list_item", _create)
    monkeypatch.setattr(sd.request_store, "update_list_item_fields", _update)
    monkeypatch.setattr(sd, "approve_leave_request", _approve)
    return calls


def test_it_creates_stamps_then_approves_through_the_shared_handler(monkeypatch):
    calls = _patch(monkeypatch)

    result = asyncio.run(log_sick_day_on_behalf("42", FORM))

    assert calls["order"] == ["create", "stamp", "approve"]
    # Recorded in the employee's name, with the working days already counted.
    assert calls["created"]["SubmittedTestLookupId"] == 601
    assert calls["created"]["Days"] == 2
    assert calls["created"]["LeaveType"] == "Sick or Personal Day"
    # Stamped with the acting manager and the employee's org placement.
    request_id, stamp = calls["stamp"]
    assert request_id == "777"
    assert stamp["ManagerLookupId"] == 501
    assert stamp["StaffLocation"] == "British Columbia"
    assert stamp["AllManagersLookupId"] == [501]
    # Approved by the same handler the email link uses, as this manager.
    assert calls["approved"] == ("777", "42")
    assert result["status"] == "approved"
    assert result["request_id"] == "777"
    assert result["days"] == 2
    assert result["balances"] == {"CurrentSickDayBalance": 3}


def test_the_clash_check_covers_the_new_dates_before_anything_is_written(monkeypatch):
    calls = _patch(monkeypatch, conflict={
        "item_id": "11", "start_date": "2026-09-08", "end_date": "2026-09-10", "status": "Approved",
    })

    with pytest.raises(SickDayOnBehalfError, match="#11"):
        asyncio.run(log_sick_day_on_behalf("42", FORM))

    assert calls["overlap"]["submitter_lookup_id"] == 601
    assert calls["overlap"]["start_date"] == "2026-09-07"
    assert calls["overlap"]["days"] == 2
    assert calls["created"] is None


def test_a_manager_outside_the_gate_is_refused_before_anything_is_written(monkeypatch):
    calls = _patch(monkeypatch, manager=ON_MANAGER)

    with pytest.raises(SickDayNotPermitted):
        asyncio.run(log_sick_day_on_behalf("43", FORM))

    assert calls["created"] is None


def test_an_employee_not_on_the_managers_team_is_refused(monkeypatch):
    someone_else = {**EMPLOYEE, "fields": {**EMPLOYEE["fields"],
                    "AllManagers": [{"LookupId": 9, "LookupValue": "Toronto Boss"}]}}
    calls = _patch(monkeypatch, employee=someone_else)

    with pytest.raises(SickDayOnBehalfError, match="not assigned to you"):
        asyncio.run(log_sick_day_on_behalf("42", FORM))

    assert calls["created"] is None


def test_an_unknown_employee_is_refused(monkeypatch):
    calls = _patch(monkeypatch)

    with pytest.raises(SickDayOnBehalfError, match="could not be found"):
        asyncio.run(log_sick_day_on_behalf("42", {**FORM, "employee_id": "999"}))

    assert calls["created"] is None


def test_a_range_with_no_working_days_is_refused(monkeypatch):
    calls = _patch(monkeypatch)
    weekend = {**FORM, "start_date": "2026-09-05", "end_date": "2026-09-06"}  # Sat-Sun

    with pytest.raises(SickDayOnBehalfError, match="no working days"):
        asyncio.run(log_sick_day_on_behalf("42", weekend))

    assert calls["created"] is None


def test_an_employee_whose_email_does_not_resolve_is_refused(monkeypatch):
    # Approval resolves the employee from the person field, so an unresolvable
    # identity would create a request that can never be approved.
    calls = _patch(monkeypatch, employee_resolves=False)

    with pytest.raises(SickDayOnBehalfError, match="Microsoft 365"):
        asyncio.run(log_sick_day_on_behalf("42", FORM))

    assert calls["created"] is None


def test_a_refused_approval_names_the_request_left_pending(monkeypatch):
    calls = _patch(monkeypatch, approve_result={"error": "Employee not found"})

    with pytest.raises(SickDayOnBehalfError, match="#777"):
        asyncio.run(log_sick_day_on_behalf("42", FORM))

    # The request was created and stamped, so it is visible in the Pending list.
    assert calls["order"] == ["create", "stamp", "approve"]


# ----- the endpoint -----

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    app = FastAPI()
    app.include_router(dashboard_router)
    return TestClient(app, raise_server_exceptions=False)


def _as(role, uid="42"):
    """Query params for a signed dashboard link, the way the React app sends them."""
    return generate_dashboard_token(role, uid)


def test_a_manager_can_log_a_sick_day(client, monkeypatch):
    seen = {}

    async def _fake(manager_id, form_data):
        seen["manager_id"] = manager_id
        seen["form"] = form_data
        return {"status": "approved", "request_id": "777", "employee_name": "Roland Alvarez",
                "days": 2, "balances": {}}

    monkeypatch.setattr(sd, "log_sick_day_on_behalf", _fake)

    resp = client.post("/team/sick-days", params=_as("manager"), json=FORM)

    assert resp.status_code == 200, resp.text
    assert resp.json()["request_id"] == "777"
    assert seen["manager_id"] == "42"          # the manager is the token's uid, never the body
    assert seen["form"]["employee_id"] == "480"


def test_an_employee_token_cannot_reach_it(client):
    resp = client.post("/team/sick-days", params=_as("employee"), json=FORM)
    assert resp.status_code == 403


def test_no_token_is_rejected(client):
    resp = client.post("/team/sick-days", json=FORM)
    assert resp.status_code == 422           # the token params are required


def test_a_location_outside_the_gate_is_a_403(client, monkeypatch):
    async def _refuse(manager_id, form_data):
        raise SickDayNotPermitted("not enabled for your location")

    monkeypatch.setattr(sd, "log_sick_day_on_behalf", _refuse)

    resp = client.post("/team/sick-days", params=_as("manager"), json=FORM)

    assert resp.status_code == 403
    assert "location" in resp.json()["detail"]


def test_a_refusal_becomes_a_readable_400(client, monkeypatch):
    async def _refuse(manager_id, form_data):
        raise SickDayOnBehalfError("Roland Alvarez is not assigned to you")

    monkeypatch.setattr(sd, "log_sick_day_on_behalf", _refuse)

    resp = client.post("/team/sick-days", params=_as("manager"), json=FORM)

    assert resp.status_code == 400
    assert "not assigned" in resp.json()["detail"]


def test_processing_disabled_is_a_503(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)

    resp = client.post("/team/sick-days", params=_as("manager"), json=FORM)

    assert resp.status_code == 503


def test_team_members_carries_the_gate_flag(client, monkeypatch):
    # The dashboard reads this flag to decide whether to show the button, so a
    # BC manager gets True and an Ontario manager gets False from the same call.
    directory = {"42": BC_MANAGER, "43": ON_MANAGER, "480": EMPLOYEE}

    async def _get_by_id(item_id):
        return directory.get(str(item_id))

    async def _get_all(list_id, **kwargs):
        return list(directory.values())

    monkeypatch.setattr(dashboard_route, "get_employee_by_id", _get_by_id)
    monkeypatch.setattr(dashboard_route.sp_client, "get_list_items", _get_all)

    bc = client.get("/team/members", params=_as("manager", "42")).json()
    on = client.get("/team/members", params=_as("manager", "43")).json()

    assert bc["can_log_sick_days"] is True
    assert [m["name"] for m in bc["members"]] == ["Roland Alvarez"]
    assert on["can_log_sick_days"] is False
