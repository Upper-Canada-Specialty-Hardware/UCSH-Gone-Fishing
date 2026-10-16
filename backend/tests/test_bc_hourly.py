"""hourly staff (#300): sick is the only balance, vacation is a counter.

Covers the rules in services/employment.py and every place that reads them:
the balance simulation, approval and refund, the request page preview, the
"Taken from" chips, the setup check and the admin edits. A red "below zero"
flag is raised for everyone when approving digs the floor (sick for BC
hourly, vacation for the rest) under zero; it never blocks.
"""

import asyncio
import json
import random
from datetime import date

import pytest
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.request_item import LeaveRequestItem
from app.repositories.request_store import request_list_tables, request_store
from app.services import employment, leave_requests
from app.services.balance import simulate_leave_impact
from app.services.employee_creation import EmployeeValidationError, build_employee_fields
from app.services.request_intake import parse_request_form
from app.services.request_preview import build_preview
from app.services.staff_entitlements import plan_changes
from app.services.taken_from import taken_from_for

TODAY = date(2026, 11, 2)

# An hourly person with 2 sick days left and a stray -3.5 vacation left over
# from before #300 (when they were run as salaried).
BC = {
    "Title": "Jamie Sample", "EmailAddress": "jamie@ucsh.com", "Location": "British Columbia",
    "SalaryHourly": "Hourly BC", "CurrentVacationBalance": -3.5, "CurrentSickDayBalance": 2,
    "CurrentOvertimeBalance": 1, "CarryOver": 0, "Payout": 0,
    "DefaultYearlyVacationDays": 0, "SickDayEntitlement": 5,
}
SALARY = {**BC, "SalaryHourly": "Salary", "CurrentVacationBalance": 1, "DefaultYearlyVacationDays": 15}


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _form(kind, **body):
    """A checked request-page form."""
    return parse_request_form(kind, body)


# ----- the rules -----

def test_which_kinds_cost_a_balance_per_type():
    ontario = {"SalaryHourly": "Hourly"}
    assert employment.leave_costs_balance(BC, "Sick or Personal Day")
    assert employment.leave_costs_balance(BC, "Half Day or Partial Day Off")   # a whole sick day
    assert not employment.leave_costs_balance(BC, "Vacation")                  # counted only
    # Every hourly employee follows the same rules, wherever they work (2026-10-16).
    assert employment.leave_costs_balance(ontario, "Half Day or Partial Day Off")      # a whole sick day too
    assert not employment.leave_costs_balance(ontario, "Vacation")                     # counted only
    assert employment.is_hourly(ontario) and employment.is_hourly(BC)
    assert employment.floor_field(ontario) == "CurrentSickDayBalance"                  # sick is the floor
    assert employment.leave_costs_balance(SALARY, "Vacation")
    assert not employment.overtime_costs_balance(BC)
    assert not employment.overtime_costs_balance(ontario)
    assert employment.overtime_costs_balance(SALARY)


def test_sick_days_are_whole_for_bc():
    assert employment.whole_sick_days(0.5) == 1
    assert employment.whole_sick_days(2) == 2
    assert employment.whole_sick_days(2.25) == 3
    assert employment.whole_sick_days(0) == 0


def test_bc_sick_takes_sick_only_and_may_go_below_zero():
    out = simulate_leave_impact(BC, "Sick or Personal Day", 3, False)
    assert out["CurrentSickDayBalance"] == -1                 # no overflow into anything
    assert out["CurrentOvertimeBalance"] == 1
    assert out["CurrentVacationBalance"] == -3.5              # untouched


def test_bc_part_day_uses_a_whole_sick_day():
    out = simulate_leave_impact(BC, "Half Day or Partial Day Off", 0.5, False)
    assert out["CurrentSickDayBalance"] == 1


def test_bc_vacation_moves_nothing():
    assert simulate_leave_impact(BC, "Vacation", 3, False) is None


def test_floor_warning_uses_sick_for_bc_and_vacation_for_everyone_else():
    bc_projected = simulate_leave_impact(BC, "Sick or Personal Day", 3, False)
    assert employment.floor_warning(BC, bc_projected) == {"pot": "sick", "before": 2, "after": -1}
    sal_projected = simulate_leave_impact(SALARY, "Vacation", 4, False)   # 1 make-up, then vacation
    warning = employment.floor_warning(SALARY, sal_projected)
    assert warning["pot"] == "vacation" and warning["after"] < 0
    assert employment.floor_warning(SALARY, simulate_leave_impact(SALARY, "Vacation", 1, False)) is None


def test_floor_warning_ignores_a_balance_already_negative_that_is_not_touched():
    projected = simulate_leave_impact(BC, "Sick or Personal Day", 1, False)
    assert employment.floor_warning(BC, projected) is None    # vacation -3.5 is not BC's floor


def test_vacation_counter_counts_approved_vacation_in_the_year():
    rows = [
        {"Status": "Approved", "LeaveType": "Vacation", "Days": 2, "StartDate": "2026-03-02"},
        {"Status": "Approved", "LeaveType": "Vacation", "Days": 1, "StartDate": "2026-11-02"},
        {"Status": "Pending", "LeaveType": "Vacation", "Days": 5, "StartDate": "2026-12-01"},
        {"Status": "Refunded", "LeaveType": "Vacation", "Days": 4, "StartDate": "2026-05-01"},
        {"Status": "Approved", "LeaveType": "Sick or Personal Day", "Days": 1, "StartDate": "2026-06-01"},
        {"Status": "Approved", "LeaveType": "Vacation", "Days": 3, "StartDate": "2025-12-01"},
    ]
    assert employment.vacation_days_counted(rows, 2026) == 3


# ----- request page preview -----

def test_preview_counts_bc_vacation():
    form = _form("leave", leave_type="Vacation", start_date="2030-11-12", end_date="2030-11-12")
    out = build_preview("leave", form, BC, [])
    assert out["projected"] is None
    assert out["unchanged"] == "Counted only. No balance is used."
    assert out["below_zero"] is None


def test_preview_flags_bc_sick_below_zero_in_red_but_does_not_block():
    form = _form("leave", leave_type="Sick or Personal Day", start_date="2030-11-11", end_date="2030-11-13")
    out = build_preview("leave", form, BC, [])
    assert out["projected"]["CurrentSickDayBalance"] == -1
    assert out["below_zero"]["pot"] == "sick"
    assert out["warning"] == ""                                # not an automatic rejection


def test_preview_notes_a_bc_part_day_is_a_whole_sick_day():
    form = _form("leave", leave_type="Half Day or Partial Day Off", start_date="2030-11-12",
                 end_date="2030-11-12", partial_hours=4)
    out = build_preview("leave", form, BC, [])
    assert out["projected"]["CurrentSickDayBalance"] == 1
    assert any("whole sick day" in n for n in out["notes"])


def test_preview_refuses_bc_carry_over():
    form = _form("carryover-payout", type_of_request="Carry Over", days=1)
    out = build_preview("carryover-payout", form, BC, [])
    assert out["projected"] is None and out["warning"] == "Hourly staff have no vacation balance to carry over or pay out."


# ----- taken from -----

def _row(**fields):
    """One leave request row for taken_from_for."""
    return {"LeaveType": "Vacation", "Days": 2, "StartDate": "2026-11-02", "EndDate": "2026-11-03", **fields}


def test_bc_vacation_reads_counted():
    for status in ("Pending", "Approved"):
        out = taken_from_for(_row(Status=status), "leave", BC, TODAY)
        assert out == {"state": "counted", "pieces": [], "counted_days": 2}


def test_bc_vacation_approved_before_300_shows_what_it_really_did():
    log = json.dumps([{"action": "approve", "steps": [
        {"before": {"CurrentVacationBalance": 0}, "after": {"CurrentVacationBalance": -2}},
    ]}])
    out = taken_from_for(_row(Status="Approved", BalanceAuditLog=log), "leave", BC, TODAY)
    assert out["state"] == "recorded"
    assert out["pieces"] == [{"pot": "vacation", "days": -2, "year": None}]


def test_pending_projection_carries_the_below_zero_flag():
    out = taken_from_for(_row(Status="Pending", LeaveType="Sick or Personal Day", Days=3), "leave", BC, TODAY)
    assert out["state"] == "projected"
    assert out["pieces"] == [{"pot": "sick", "days": -3, "year": None}]
    assert out["below_zero"] == {"pot": "sick", "before": 2, "after": -1}


# ----- admin screens -----

def test_bc_vacation_entitlement_may_be_zero_but_not_negative():
    assert plan_changes(BC, {"vacation_entitlement": 0}) == []          # already 0: no change
    changes = plan_changes({**BC, "DefaultYearlyVacationDays": 2}, {"vacation_entitlement": 0})
    assert changes[0]["after"] == 0
    with pytest.raises(ValueError, match="below zero"):
        plan_changes(BC, {"vacation_entitlement": -1})
    with pytest.raises(ValueError, match="above zero"):
        plan_changes(SALARY, {"vacation_entitlement": 0})               # salaried still need one
    # The same save switching the type to hourly allows the zero too.
    assert plan_changes(SALARY, {"vacation_entitlement": 0}, "Hourly BC")[0]["after"] == 0


def _creation_form(**over):
    """An Add Employee form for an hourly person."""
    return {
        "title": "Taylor Mock", "email_address": "taylor@ucsh.com", "location": "British Columbia",
        "department": "Warehouse", "salary_hourly": "Hourly BC",
        "vacation_entitlement": 0, "sick_entitlement": 5, **over,
    }


def test_add_employee_accepts_hourly_bc_with_no_vacation():
    fields = build_employee_fields(_creation_form())
    assert fields["SalaryHourly"] == "Hourly BC"
    assert fields["DefaultYearlyVacationDays"] == 0
    with pytest.raises(EmployeeValidationError):
        build_employee_fields(_creation_form(salary_hourly="Salary"))   # salaried still need vacation


# ----- approval and refund, against the Postgres request store -----

@pytest.fixture
def bc_store(monkeypatch):
    """An hourly submitter, requests in Postgres, Staff Directory writes captured."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    record = {"id": "7", "fields": dict(BC)}                  # mutated by the fake writes
    boss = {"id": "3", "fields": {"Title": "Sam Supervisor", "EmailAddress": "sam@ucsh.com"}}

    async def wipe():
        async with async_session() as session:
            await session.execute(delete(LeaveRequestItem))
            await session.commit()

    async def submitter(fields, column):
        return record

    async def by_id(employee_id):
        return boss if str(employee_id) == "3" else record

    async def staff_write(list_id, item_id, updates):
        assert list_id == settings.SP_LIST_STAFF_DIRECTORY        # only the staff record via SharePoint
        record["fields"].update(updates)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(sharepoint.sp_client, "update_list_item_fields", staff_write)
    monkeypatch.setattr(leave_requests, "resolve_request_submitter", submitter)
    monkeypatch.setattr(leave_requests, "get_employee_by_id", by_id)
    monkeypatch.setattr(leave_requests, "send_email_with_dashboard", nothing)
    monkeypatch.setattr(leave_requests, "notify_requests_blocked_by_approval", nothing)
    monkeypatch.setattr(leave_requests, "recalculate_request_allow_date", nothing)
    run(wipe())
    yield record
    run(wipe())


def _pending(leave_type: str, days: float) -> str:
    """Store a pending leave request; return its id."""
    item_id = random.randint(10_000_000, 99_999_999)          # the claim log outlives the wipe
    table = request_list_tables()[settings.SP_LIST_LEAVE_REQUESTS]
    run(table.create_list_item(settings.SP_LIST_LEAVE_REQUESTS, {
        "LeaveType": leave_type, "Status": "Pending", "StartDate": "2026-11-02",
        "EndDate": "2026-11-04", "Days": days, "SubmitterEmail": "jamie@ucsh.com",
    }, item_id=item_id))
    return str(item_id)


def test_approving_bc_sick_takes_whole_days_from_sick_only(bc_store):
    request_id = _pending("Sick or Personal Day", 3)
    result = run(leave_requests.approve_leave_request(request_id, "3"))
    assert result["status"] == "approved"
    f = bc_store["fields"]
    assert f["CurrentSickDayBalance"] == -1                    # below zero, no overflow
    assert f["CurrentVacationBalance"] == -3.5 and f["CurrentOvertimeBalance"] == 1


def test_approving_bc_vacation_moves_nothing(bc_store):
    request_id = _pending("Vacation", 3)
    assert run(leave_requests.approve_leave_request(request_id, "3")) == {"status": "approved", "hourly": True}
    assert bc_store["fields"] == BC


def test_refunding_bc_sick_puts_the_days_back(bc_store):
    request_id = _pending("Half Day or Partial Day Off", 0.5)
    run(leave_requests.approve_leave_request(request_id, "3"))
    assert bc_store["fields"]["CurrentSickDayBalance"] == 1
    item = run(request_store.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id))
    assert item["fields"]["Status"] == "Approved"
    run(leave_requests.refund_leave_request(request_id, "3"))
    assert bc_store["fields"]["CurrentSickDayBalance"] == 2
