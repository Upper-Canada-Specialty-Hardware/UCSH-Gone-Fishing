"""Which balance pots each request's days came from, or went into (#296).

Two layers: the pure ``taken_from_for`` against hand-built audit logs and
employee balances (the bulk, since the math lives there), and one presence test
per endpoint family proving the ``taken_from`` key is attached to every row.
"""

import json

import pytest
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import dashboard
from app.routes.dashboard import DashboardUser, _PersonNames, get_dashboard_user
from app.services.taken_from import _earned_year, taken_from_for

# A fixed "today" so the pending-projection year is deterministic: November is
# past April 1, so a carry-over/payout earned now reads as the current year.
TODAY = date(2026, 11, 2)

# A salaried employee with a day in every spending pot, used across the tests.
EMP = {
    "Title": "Pat Staff", "EmailAddress": "pat@ucsh.com", "SalaryHourly": "Salary",
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 1, "Payout": 0,
}
HOURLY = {**EMP, "SalaryHourly": "Hourly"}


def _step(op, before, after):
    """One audit step: the operation name and the before/after balance snapshots."""
    return {"operation": op, "before": before, "after": after}


def _log(steps, action="approve", extra=None):
    """A BalanceAuditLog JSON string of one entry (plus an optional later one).

    Args:
        steps: The entry's steps (from _step).
        action: The entry's action, "approve" by default.
        extra: An optional second entry {"action", "steps"} appended after it
            (e.g. a refund entry that follows the approve entry).

    Returns:
        The audit log as a JSON string, as SharePoint stores it.
    """
    entries = [{"action": action, "timestamp": "2026-11-02T10:00:00", "steps": steps}]
    if extra:
        entries.append(extra)
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# Pure function: recorded (approved), from the audit log
# ---------------------------------------------------------------------------

def test_vacation_recorded_spends_make_up_carryover_and_vacation():
    # Vacation, 5 days: Make-Up 2 -> -3, cascade to CarryOver 1 -> 0 and
    # Vacation 10 -> 8. Net: make_up -2, carry_over -1, vacation -2.
    audit = _log([
        _step("Deduct Vacation from Make-Up",
              {"CurrentOvertimeBalance": 2}, {"CurrentOvertimeBalance": -3}),
        _step("Cascade (current year)",
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": -3, "CarryOver": 1, "CurrentVacationBalance": 10},
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": 0, "CarryOver": 0, "CurrentVacationBalance": 8}),
    ])
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "make_up", "days": -2.0, "year": None},
        {"pot": "carry_over", "days": -1.0, "year": 2026},
        {"pot": "vacation", "days": -2.0, "year": None},
    ]


def test_sick_within_balance_recorded():
    audit = _log([
        _step("Deduct Sick/Personal from Sick",
              {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3}),
    ])
    item = {"Status": "Approved", "LeaveType": "Sick or Personal Day", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


def test_next_year_request_makes_carryover_negative_recorded():
    # Next-year vacation, 3 days: Make-Up 1 -> -2, cascade Make-Up -> CarryOver,
    # leaving CarryOver at -2. Net: make_up -1, carry_over -2.
    audit = _log([
        _step("Deduct Vacation from Make-Up",
              {"CurrentOvertimeBalance": 1}, {"CurrentOvertimeBalance": -2}),
        _step("Cascade (next year)",
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": -2, "CarryOver": 0, "CurrentVacationBalance": 10},
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": 0, "CarryOver": -2, "CurrentVacationBalance": 10}),
    ])
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["pieces"] == [
        {"pot": "make_up", "days": -1.0, "year": None},
        {"pot": "carry_over", "days": -2.0, "year": 2026},
    ]


def test_carryover_request_feb_approval_is_previous_year():
    # Carry Over adds to CarryOver; a Feb approval is still last year's carry-over.
    audit = _log([
        _step("Transfer Vacation to Carry Over",
              {"CurrentVacationBalance": 10, "CarryOver": 0}, {"CurrentVacationBalance": 7, "CarryOver": 3}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Carry Over", "ApprovedDate": "2026-02-15", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "carry_over", "days": 3.0, "year": 2025},   # Feb -> previous year
        {"pot": "vacation", "days": -3.0, "year": None},
    ]


def test_carryover_request_may_approval_is_same_year():
    audit = _log([
        _step("Transfer Vacation to Carry Over",
              {"CurrentVacationBalance": 10, "CarryOver": 0}, {"CurrentVacationBalance": 7, "CarryOver": 3}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Carry Over", "ApprovedDate": "2026-05-10", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["pieces"][0] == {"pot": "carry_over", "days": 3.0, "year": 2026}  # May -> same year


def test_payout_recorded_names_the_year():
    audit = _log([
        _step("Transfer Vacation to Payout",
              {"CurrentVacationBalance": 10, "Payout": 0}, {"CurrentVacationBalance": 8, "Payout": 2}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Payout", "ApprovedDate": "2026-05-10", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["pieces"] == [
        {"pot": "vacation", "days": -2.0, "year": None},
        {"pot": "payout", "days": 2.0, "year": 2026},
    ]


def test_overtime_recorded_adds_make_up_and_pays_back_vacation():
    # 2 days overtime added, then offset against a -1 vacation: net make_up +1,
    # vacation +1.
    audit = _log([
        _step("Add overtime to Make-Up",
              {"CurrentOvertimeBalance": 0}, {"CurrentOvertimeBalance": 2}),
        _step("Vacation offset",
              {"CurrentVacationBalance": -1, "CurrentOvertimeBalance": 2},
              {"CurrentVacationBalance": 0, "CurrentOvertimeBalance": 1}),
    ])
    item = {"Status": "Approved", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "overtime", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "make_up", "days": 1.0, "year": None},
        {"pot": "vacation", "days": 1.0, "year": None},
    ]


# ---------------------------------------------------------------------------
# Pure function: projected (pending)
# ---------------------------------------------------------------------------

def test_pending_vacation_projected():
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "projected"
    assert result["pieces"] == [
        {"pot": "make_up", "days": -2.0, "year": None},
        {"pot": "carry_over", "days": -1.0, "year": 2026},
        {"pot": "vacation", "days": -2.0, "year": None},
    ]


def test_pending_overtime_projection_rounds_to_three_decimals():
    # 5 hours / 8 = 0.625 of a day into Make-Up; vacation is positive so no offset.
    emp = {**EMP, "CurrentVacationBalance": 5, "CurrentOvertimeBalance": 0}
    item = {"Status": "Pending", "Hours": 5}
    result = taken_from_for(item, "overtime", emp, TODAY)
    assert result["pieces"] == [{"pot": "make_up", "days": 0.625, "year": None}]


def test_pending_hourly_vacation_is_none():
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    assert taken_from_for(item, "leave", HOURLY, TODAY) == {"state": "none", "pieces": []}


# ---------------------------------------------------------------------------
# Pure function: none / unrecorded / refunded
# ---------------------------------------------------------------------------

def test_approved_hourly_vacation_is_none():
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02"}
    assert taken_from_for(item, "leave", HOURLY, TODAY) == {"state": "none", "pieces": []}


def test_bereavement_is_none():
    item = {"Status": "Approved", "LeaveType": "Bereavement", "ApprovedDate": "2026-11-02"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "none", "pieces": []}


def test_approved_without_audit_is_unrecorded():
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "unrecorded", "pieces": []}


def test_rejected_is_none():
    item = {"Status": "Rejected", "LeaveType": "Vacation"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "none", "pieces": []}


def test_refunded_uses_the_original_approval_deltas():
    # An approve entry then a refund entry; the refunded state shows the approve
    # entry's deltas, not the reversal.
    audit = _log(
        [_step("Deduct Sick/Personal from Sick",
               {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3})],
        extra={"action": "refund", "steps": [
            _step("Refund", {"CurrentSickDayBalance": 3}, {"CurrentSickDayBalance": 5})]},
    )
    item = {"Status": "Refunded", "LeaveType": "Sick or Personal Day", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "refunded"
    assert result["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


def test_earned_year_boundary():
    assert _earned_year(date(2026, 1, 1)) == 2025
    assert _earned_year(date(2026, 3, 31)) == 2025     # still last year's carry-over
    assert _earned_year(date(2026, 4, 1)) == 2026      # from April 1, the new year
    assert _earned_year(date(2026, 12, 31)) == 2026


def test_unresolved_employee_on_pending_is_unrecorded():
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    assert taken_from_for(item, "leave", None, TODAY) == {"state": "unrecorded", "pieces": []}


# ---------------------------------------------------------------------------
# Endpoints: the key is present on every row
# ---------------------------------------------------------------------------

MANAGER = {"id": "9", "fields": {"Title": "Boss One", "EmailAddress": "boss@ucsh.com"}}
EMP_ITEM = {"id": "7", "fields": {**EMP, "AllManagers": [{"LookupValue": "Boss One"}]}}

# An approved leave (recorded) and a fully-processed pending leave (projected),
# both resolving to Pat Staff through SubmitterEmail.
APPROVED_LEAVE = {"id": "100", "fields": {
    "Status": "Approved", "LeaveType": "Sick or Personal Day", "SubmitterEmail": "pat@ucsh.com",
    "ApprovedDate": "2026-11-02", "Days": 2,
    "BalanceAuditLog": _log([_step("Deduct Sick/Personal from Sick",
                                   {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3})]),
}}
PENDING_LEAVE = {"id": "101", "fields": {
    "Status": "Pending", "LeaveType": "Vacation", "SubmitterEmail": "pat@ucsh.com",
    "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5, "ManagerLookupId": 55,
}}


def _lookups():
    """The four maps _build_staff_lookups returns, built for Pat Staff."""
    sp_user_to_name = _PersonNames()
    sp_user_to_name.by_email["pat@ucsh.com"] = "Pat Staff"
    return (
        {"pat staff": EMP_ITEM},            # staff_by_name (lowercased)
        {7: EMP_ITEM},                      # staff_by_id
        sp_user_to_name,                    # sp_user_to_name (+ by_email)
        {"Boss One": {"Pat Staff"}},        # mgr_to_emp_names
    )


@pytest.fixture
def mounted(monkeypatch):
    """The dashboard router with the Staff Directory and request lists stubbed.

    Returns a factory: ``mounted(role, uid, leave_items)`` gives a TestClient
    whose auth is overridden to that role/uid and whose leave list returns the
    given items (the other two request lists are empty).
    """
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def fake_lookups():
        return _lookups()

    async def fake_by_id(eid):
        return {"7": EMP_ITEM, "9": MANAGER}.get(str(eid))

    monkeypatch.setattr(dashboard, "_build_staff_lookups", fake_lookups)
    monkeypatch.setattr(dashboard, "get_employee_by_id", fake_by_id)

    def factory(role, uid, leave_items):
        async def fake_get_list_items(list_id, *a, **k):
            return list(leave_items) if list_id == settings.SP_LIST_LEAVE_REQUESTS else []

        monkeypatch.setattr(dashboard.request_store, "get_list_items", fake_get_list_items)

        app = FastAPI()
        app.include_router(dashboard.router)
        app.dependency_overrides[get_dashboard_user] = lambda: DashboardUser(role=role, user_id=uid)
        return TestClient(app, raise_server_exceptions=False)

    return factory


def _states(rows):
    """The set of taken_from states across rows, asserting the key is on each."""
    assert rows, "expected at least one row"
    for r in rows:
        assert "taken_from" in r
        assert r["taken_from"]["state"] in ("recorded", "projected", "none", "unrecorded", "refunded")
    return {r["taken_from"]["state"] for r in rows}


def test_me_requests_carry_taken_from(mounted):
    client = mounted("employee", "7", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/me/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_team_requests_carry_taken_from(mounted):
    client = mounted("manager", "9", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/team/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_team_pending_carry_taken_from(mounted):
    client = mounted("manager", "9", [PENDING_LEAVE])
    resp = client.get("/team/pending")
    assert resp.status_code == 200
    assert _states(resp.json()["pending"]) == {"projected"}


def test_admin_requests_carry_taken_from(mounted):
    client = mounted("admin", "7", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/admin/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_admin_pending_carry_taken_from(mounted):
    client = mounted("admin", "7", [PENDING_LEAVE])
    resp = client.get("/admin/pending")
    assert resp.status_code == 200
    assert _states(resp.json()["pending"]) == {"projected"}


def test_admin_stuck_requests_carry_taken_from(mounted, monkeypatch):
    # A pending leave with no manager is "stuck". Its approval-email record reads
    # as unknown, and there is no conflict to find.
    stuck_item = {"id": "102", "fields": {
        "Status": "Pending", "LeaveType": "Vacation", "SubmitterEmail": "pat@ucsh.com",
        "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 0,
    }}

    async def fake_emailed(_entries):
        return None

    monkeypatch.setattr(dashboard, "fetch_approval_email_records", fake_emailed)
    client = mounted("admin", "7", [stuck_item])
    resp = client.get("/admin/stuck-requests")
    assert resp.status_code == 200
    rows = resp.json()["stuck"]
    assert rows and all("taken_from" in r for r in rows)
