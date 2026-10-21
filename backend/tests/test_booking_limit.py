"""The vacation booking policy and the carry-over math it fixes (#407).

The rule lives in one pure function (``booking_limit``), the carry-over expiry in
another (``carryover_available_for``), and the cascade no longer has a separate
next-year path. These tests pin the pure math (with a fixed "today" so the year
boundaries are deterministic) and then the three enforcement points that read
it: submission, the admin carry-over fix, and the preview.
"""

import asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import admin_requests
from app.services import request_intake
from app.services.balance import (
    booking_limit,
    booking_limit_message,
    booking_refusal,
    carryover_available_for,
    carryover_expiry,
    cascade_current_year_pure,
    simulate_leave_impact,
)
from app.services.request_intake import RequestFormError, parse_request_form, submit_request


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


# A day in the Apr-Dec part of the year: carry-over expires March 31 NEXT year.
OCT_2026 = date(2026, 10, 21)
# A day in the Jan-Mar part of the year: carry-over expires March 31 THIS year.
FEB_2026 = date(2026, 2, 10)


# --- the booking limit, the one rule, with its year boundaries ---

def test_booking_limit_only_carryover_is_end_of_this_year():
    # V == 0 and C > 0 -> Dec 31 this year.
    assert booking_limit(0, 5, OCT_2026) == date(2026, 12, 31)


def test_booking_limit_vacation_left_is_end_of_next_years_carryover_window():
    # V > 0 and C == 0 -> Mar 31 next year; V > 0 and C > 0 -> the same.
    assert booking_limit(10, 0, OCT_2026) == date(2027, 3, 31)
    assert booking_limit(10, 5, OCT_2026) == date(2027, 3, 31)


def test_booking_limit_nothing_left_is_end_of_next_year():
    # V == 0 and C == 0 -> Dec 31 next year.
    assert booking_limit(0, 0, OCT_2026) == date(2027, 12, 31)


def test_booking_limit_treats_negative_vacation_and_carryover_as_zero():
    # V < 0 counts as 0 (only carry-over left); C <= 0 counts as 0.
    assert booking_limit(-3, 5, OCT_2026) == date(2026, 12, 31)      # V<0 -> like V==0, C>0
    assert booking_limit(10, -5, OCT_2026) == date(2027, 3, 31)      # C<0 -> like C==0, V>0
    assert booking_limit(0, -5, OCT_2026) == date(2027, 12, 31)      # both effectively 0


def test_booking_limit_moves_with_the_calendar_year():
    # "This year" and "next year" follow today's Toronto year.
    assert booking_limit(0, 5, date(2027, 1, 10)) == date(2027, 12, 31)   # only carry-over, in 2027
    assert booking_limit(10, 0, date(2027, 1, 10)) == date(2028, 3, 31)   # vacation left, in 2027


def test_booking_limit_message_names_the_date():
    assert booking_limit_message(date(2027, 3, 31)) == "You can book vacation until March 31, 2027."


# --- carry-over expiry, both halves of the year ---

def test_carryover_expiry_is_march_31_this_year_while_still_ahead():
    assert carryover_expiry(FEB_2026) == date(2026, 3, 31)          # before Mar 31: this year
    assert carryover_expiry(date(2026, 3, 31)) == date(2026, 3, 31)  # on the day: still this year


def test_carryover_expiry_is_next_year_once_march_has_passed():
    assert carryover_expiry(OCT_2026) == date(2027, 3, 31)


def test_carryover_available_only_when_no_day_is_past_expiry():
    # Apr-Dec: expiry Mar 31 next year.
    assert carryover_available_for(date(2027, 3, 31), OCT_2026) is True
    assert carryover_available_for(date(2027, 4, 1), OCT_2026) is False
    # Jan-Mar: expiry Mar 31 this year.
    assert carryover_available_for(date(2026, 3, 31), FEB_2026) is True
    assert carryover_available_for(date(2026, 4, 1), FEB_2026) is False
    assert carryover_available_for(None, OCT_2026) is True          # no dates: allowed


# --- the cascade: carry-over never negative, and skipped past expiry ---

def test_carry_over_never_goes_negative_from_spending():
    # Carry Over 5, Make-Up 0, spend an 8-day vacation: Make-Up -> -8 overflows
    # into Carry Over, which pays its 5 down to 0 (never below) and passes the
    # rest to Vacation. Carry-over ends at 0, Vacation absorbs the shortfall.
    result = cascade_current_year_pure(sick=5, overtime=-8, carryover=5, vacation=10)
    assert result["CarryOver"] == 0
    assert result["CurrentOvertimeBalance"] == 0
    assert result["CurrentVacationBalance"] == 7                    # 10 - (8 - 5)


def test_cascade_skips_carry_over_when_it_has_expired_for_the_dates():
    # carryover_available False: the Make-Up deficit goes straight to Vacation and
    # carry-over is left untouched.
    result = cascade_current_year_pure(
        sick=5, overtime=-3, carryover=5, vacation=10, carryover_available=False,
    )
    assert result["CarryOver"] == 5                                 # untouched
    assert result["CurrentVacationBalance"] == 7                    # took the whole 3 days


def test_simulate_leave_skips_carry_over_past_expiry_both_halves_of_the_year():
    # A salaried person with carry-over, a 3-day vacation dated past the expiry:
    # carry-over pays none of it in either half of the year.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 5,
           "CurrentOvertimeBalance": 0, "CurrentSickDayBalance": 5}
    # Apr-Dec case (today Oct 2026, a request in Apr 2027).
    apr_dec = simulate_leave_impact(emp, "Vacation", 3, carryover_available=False)
    assert apr_dec["CarryOver"] == 5 and apr_dec["CurrentVacationBalance"] == 7
    # Within the window, carry-over does pay.
    within = simulate_leave_impact(emp, "Vacation", 3, carryover_available=True)
    assert within["CarryOver"] == 2 and within["CurrentVacationBalance"] == 10


def test_jeffs_case_five_next_year_vacations_spend_vacation_not_carry_over():
    # Jeff: V = 11, C = 0 in Oct 2026, five 1-day vacations in Jan-Mar 2027.
    # Those dates are within the carry-over window, but C is 0, so each day comes
    # out of Vacation: 11 -> 6, and carry-over never goes negative.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 11, "CarryOver": 0,
           "CurrentOvertimeBalance": 0, "CurrentSickDayBalance": 0}
    for _ in range(5):
        projected = simulate_leave_impact(emp, "Vacation", 1, carryover_available=True)
        emp = {**emp, **projected}                                  # feed the result into the next day
    assert emp["CurrentVacationBalance"] == 6
    assert emp["CarryOver"] == 0


def test_next_year_vacation_spends_vacation_once_make_up_runs_out():
    # The old next-year path never touched Vacation; now it does. Make-Up 1, a
    # 3-day vacation: Make-Up 1 -> 0 covers one day, Vacation takes the other two.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 0,
           "CurrentOvertimeBalance": 1, "CurrentSickDayBalance": 0}
    projected = simulate_leave_impact(emp, "Vacation", 3, carryover_available=True)
    assert projected["CurrentOvertimeBalance"] == 0
    assert projected["CurrentVacationBalance"] == 8                 # 10 - 2
    assert projected["CarryOver"] == 0                              # never negative


def test_cascade_leaves_a_pre_existing_negative_carry_over_in_place():
    # Dexter: Sick 5, Make-Up 0, Carry Over -5, Vacation 14. A 1-day sick request
    # takes Sick 5 -> 4, then the cascade runs. The stored -5 carry-over is NOT
    # moved onto vacation here (that is the setup fix's job, with a reason); only a
    # deficit THIS request creates would cascade. So carry-over stays -5 and
    # vacation stays 14 - not a phantom 5 vacation days taken (#407 review).
    result = cascade_current_year_pure(sick=4, overtime=0, carryover=-5, vacation=14)
    assert result["CurrentSickDayBalance"] == 4
    assert result["CarryOver"] == -5                               # pre-existing negative untouched
    assert result["CurrentVacationBalance"] == 14                  # no vacation taken for a sick day
    assert result["CurrentOvertimeBalance"] == 0


def test_cascade_moves_only_the_request_created_carry_over_deficit():
    # A pre-existing Carry Over -5, and a spend drives Make-Up to -3 which overflows
    # into carry-over (-5 -> -8). Only the 3 days THIS request created move onto
    # vacation; the original -5 stays for the setup fix. Carry-over ends at -5,
    # vacation drops by 3 (#407 review).
    result = cascade_current_year_pure(sick=0, overtime=-3, carryover=-5, vacation=10)
    assert result["CarryOver"] == -5                               # original negative left in place
    assert result["CurrentOvertimeBalance"] == 0
    assert result["CurrentVacationBalance"] == 7                   # 10 - 3


# --- the booking refusal helper ---

def test_booking_refusal_refuses_a_salaried_vacation_past_the_limit():
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 0}
    # V>0, C==0 -> limit Mar 31 next year; a June date is past it.
    msg = booking_refusal(emp, "Vacation", date(2027, 6, 1), OCT_2026)
    assert msg == "You can book vacation until March 31, 2027."


def test_booking_refusal_allows_dates_within_the_limit():
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 0}
    assert booking_refusal(emp, "Vacation", date(2027, 3, 31), OCT_2026) is None


def test_booking_refusal_never_limits_hourly_staff():
    emp = {"SalaryHourly": "Hourly", "CurrentVacationBalance": 10, "CarryOver": 0}
    assert booking_refusal(emp, "Vacation", date(2030, 1, 1), OCT_2026) is None


def test_booking_refusal_ignores_leave_types_that_are_not_booked():
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 0}
    assert booking_refusal(emp, "Sick or Personal Day", date(2030, 1, 1), OCT_2026) is None


# --- submission refuses a past-limit request before any row is created ---

def _salaried(**over):
    """A salaried staff record with balances, for submit_request."""
    base = {"Title": "Pat Staff", "EmailAddress": "pat@ucsh.com", "SalaryHourly": "Salary",
            "CurrentVacationBalance": 10, "CarryOver": 0}
    base.update(over)
    return {"id": "7", "fields": base}


def test_submit_refuses_a_vacation_past_the_limit_and_creates_nothing(monkeypatch):
    # A June-next-year vacation for a V>0 person is past the Mar 31 limit.
    created = []

    async def must_not_create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", must_not_create)
    year = date.today().year + 1
    form = parse_request_form("leave", {
        "leave_type": "Vacation", "start_date": f"{year}-06-01", "end_date": f"{year}-06-05",
    })
    with pytest.raises(RequestFormError) as exc:
        run(submit_request("leave", form, _salaried(), "request_page"))
    assert "book vacation until" in str(exc.value)
    assert created == []                                            # refused before the SharePoint write


def test_submit_allows_a_vacation_within_the_limit(monkeypatch):
    created = []

    async def create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", create)
    today = date.today()
    form = parse_request_form("leave", {
        "leave_type": "Vacation", "start_date": today.isoformat(), "end_date": today.isoformat(),
    })
    item = run(submit_request("leave", form, _salaried(), "request_page"))
    assert item["id"] == "99" and len(created) == 1                 # it was created


# --- the admin fix for a stored negative carry-over (Dexter) ---

def test_admin_fix_moves_negative_carry_over_onto_vacation(monkeypatch):
    app = FastAPI()
    app.include_router(admin_requests.router, prefix="/api/dashboard")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    dexter = {"id": "500", "fields": {
        "Title": "Dexter", "CurrentVacationBalance": 14, "CarryOver": -5, "SalaryHourly": "Salary",
    }}

    async def by_id(i):
        return dexter if str(i) == "500" else None

    writes, history, rad = [], [], []

    async def write(list_id, emp_id, fields, **kw):
        writes.append((str(emp_id), fields))

    async def record(eid, name, changes, reason, source="admin_dashboard"):
        history.append((eid, changes, reason))

    async def recalc(eid, vacation, carryover):
        rad.append((str(eid), vacation, carryover))
        return None

    monkeypatch.setattr(admin_requests, "get_employee_by_id", by_id)
    monkeypatch.setattr(admin_requests.staff_store, "update_list_item_fields", write)
    monkeypatch.setattr(admin_requests.staff_changes, "record_changes", record)
    monkeypatch.setattr(admin_requests, "recalculate_request_allow_date", recalc)

    res = TestClient(app).post(
        "/api/dashboard/admin/staff/500/fix-negative-carryover", json={"reason": "legacy carry-over"},
    )
    assert res.status_code == 200
    body = res.json()
    # Carry-over -5 moves onto vacation: 14 -> 9, carry-over -> 0.
    assert body["after"] == {"vacation": 9.0, "carryover": 0.0}
    assert writes[0][1]["CurrentVacationBalance"] == 9.0 and writes[0][1]["CarryOver"] == 0.0
    assert {c[0] for c in history[0][1]} == {"CurrentVacationBalance", "CarryOver"}
    assert rad == [("500", 9.0, 0.0)]                               # booking limit recomputed


def test_admin_fix_refuses_a_blank_reason(monkeypatch):
    app = FastAPI()
    app.include_router(admin_requests.router, prefix="/api/dashboard")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    res = TestClient(app).post(
        "/api/dashboard/admin/staff/500/fix-negative-carryover", json={"reason": "  "},
    )
    assert res.status_code == 400


def test_admin_fix_refuses_when_carry_over_is_not_negative(monkeypatch):
    app = FastAPI()
    app.include_router(admin_requests.router, prefix="/api/dashboard")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def by_id(i):
        return {"id": "501", "fields": {"Title": "OK", "CurrentVacationBalance": 5, "CarryOver": 2}}

    monkeypatch.setattr(admin_requests, "get_employee_by_id", by_id)
    res = TestClient(app).post(
        "/api/dashboard/admin/staff/501/fix-negative-carryover", json={"reason": "no need"},
    )
    assert res.status_code == 400
