"""The vacation booking policy and the carry-over math it fixes (#407).

The refusal lives in one pure function (``booking_refusal``), the dates carry-over
can pay for in another (``leave_day_split``), and the cascade no longer has a separate
next-year path. These tests pin the pure math (with a fixed "today" so the year
boundaries are deterministic) and then the three enforcement points that read
it: submission, the admin carry-over fix, and the preview.
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.graph import sharepoint
from app.services import request_intake
from app.services.balance import (
    bookable_until,
    booking_refusal,
    booking_refusal_for,
    carryover_expiry,
    carryover_period,
    carryover_terms,
    cascade_current_year_pure,
    leave_crosses_boundary,
    leave_day_split,
    leave_split_for,
    rule_date_for,
    rule_date_from_fields,
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


# --- carry-over expiry, both halves of the year ---

def test_carryover_expiry_is_march_31_this_year_while_still_ahead():
    assert carryover_expiry(FEB_2026) == date(2026, 3, 31)          # before Mar 31: this year
    assert carryover_expiry(date(2026, 3, 31)) == date(2026, 3, 31)  # on the day: still this year


def test_carryover_expiry_is_next_year_once_march_has_passed():
    assert carryover_expiry(OCT_2026) == date(2027, 3, 31)


def test_carryover_period_is_jan_to_mar_of_the_expiry_year():
    assert carryover_period(OCT_2026) == (date(2027, 1, 1), date(2027, 3, 31))   # Apr-Dec: next year
    assert carryover_period(FEB_2026) == (date(2026, 1, 1), date(2026, 3, 31))   # Jan-Mar: this year


# --- how a request's days fall against what each pot can pay for ---

NOV_2026 = date(2026, 11, 4)
FEB_2027 = date(2027, 2, 10)
MAR_2027 = date(2027, 3, 30)
NEW_YEARS = [{"Date": "2027-01-01", "Title": "New Year"}]   # a Friday, not worked


def _zeroed(**over):
    """Salaried balances at zero, with the named ones overridden."""
    base = {"SalaryHourly": "Salary", "CurrentVacationBalance": 0, "CarryOver": 0,
            "CurrentOvertimeBalance": 0, "CurrentSickDayBalance": 0}
    base.update(over)
    return base


def test_split_a_date_this_year_is_not_for_carry_over():
    split = leave_day_split(NOV_2026, NOV_2026, 1, NOV_2026)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (0, 0, 0)


def test_split_jan_to_mar_next_year_is_ahead_of_vacation():
    split = leave_day_split(date(2027, 2, 1), date(2027, 2, 2), 2, NOV_2026)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (2, 2, 0)


def test_split_april_next_year_nothing_can_pay_yet():
    split = leave_day_split(date(2027, 4, 5), date(2027, 4, 6), 2, NOV_2026)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (0, 0, 2)


def test_split_jan_to_mar_this_year_vacation_can_still_pay():
    split = leave_day_split(FEB_2027, FEB_2027, 3, FEB_2027)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (3, 0, 0)


def test_split_rest_of_this_year_and_a_late_request_are_for_vacation():
    assert leave_day_split(date(2027, 6, 1), date(2027, 6, 1), 1, FEB_2027).carryover_days == 0
    late = leave_day_split(date(2026, 2, 3), date(2026, 2, 3), 1, NOV_2026)   # dated last Feb, filed in Nov
    assert (late.carryover_days, late.ahead_days, late.unbookable_days) == (0, 0, 0)


def test_split_next_years_jan_is_unbookable_from_jan_to_mar():
    # From Feb 2027 the period is Jan to Mar 2027, so Feb 2028 cannot be paid yet.
    split = leave_day_split(date(2028, 2, 1), date(2028, 2, 1), 1, FEB_2027)
    assert split.unbookable_days == 1 and split.carryover_days == 0


def test_split_no_dates_is_never_held_against_the_period():
    split = leave_day_split(None, None, 2, NOV_2026)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (2, 0, 0)


def test_split_crossing_new_year_counts_working_days_without_the_holiday():
    # Dec 30, Dec 31 and Jan 4; Jan 1 is a holiday and Jan 2 and 3 are the weekend.
    split = leave_day_split(date(2026, 12, 30), date(2027, 1, 4), 3, NOV_2026, NEW_YEARS)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (1, 1, 0)


def test_split_crossing_the_end_of_march_from_nov_is_partly_unbookable():
    # Mar 30 and 31 are carry-over days; Apr 1, 2027 cannot be paid yet.
    split = leave_day_split(date(2027, 3, 30), date(2027, 4, 1), 3, NOV_2026)
    assert (split.carryover_days, split.ahead_days, split.unbookable_days) == (2, 2, 1)


def test_split_is_capped_at_the_stored_days():
    # A long range with its days edited down to 1 never counts more than that.
    split = leave_day_split(date(2027, 3, 29), date(2027, 4, 2), 1, NOV_2026)
    assert split.carryover_days == 1 and split.unbookable_days == 0


def test_crossing_is_only_a_request_partly_inside_a_range():
    assert leave_crosses_boundary(date(2026, 12, 30), date(2027, 1, 4), NOV_2026) is True
    assert leave_crosses_boundary(date(2027, 3, 30), date(2027, 4, 1), MAR_2027) is True
    assert leave_crosses_boundary(date(2027, 2, 1), date(2027, 2, 5), NOV_2026) is False
    assert leave_crosses_boundary(None, None, NOV_2026) is False


def test_holidays_are_only_read_for_a_crossing_request(monkeypatch):
    async def boom(_province):
        raise AssertionError("no holiday read expected")

    monkeypatch.setattr("app.services.balance.get_holidays_for_province", boom)
    split = run(leave_split_for({"Location": "Ontario"}, date(2027, 2, 1), date(2027, 2, 2), 2, NOV_2026))
    assert split.carryover_days == 2


def test_a_holiday_read_failure_counts_weekdays_and_logs(monkeypatch, caplog):
    async def boom(_province):
        raise RuntimeError("list is down")

    monkeypatch.setattr("app.services.balance.map_location_to_province", lambda _location: "ON")
    monkeypatch.setattr("app.services.balance.get_holidays_for_province", boom)
    split = run(leave_split_for({"Location": "Ontario"}, date(2026, 12, 30), date(2027, 1, 4), 3, NOV_2026))
    # Jan 1 and Jan 4 are both weekdays, so without the holiday list 2 days are counted.
    assert split.carryover_days == 2
    assert "counting weekdays" in caplog.text


def test_an_unknown_location_counts_weekdays_rather_than_failing():
    split = run(leave_split_for({"Location": "Nowhere"}, date(2026, 12, 30), date(2027, 1, 4), 3, NOV_2026))
    assert split.carryover_days == 2


# --- which pot pays, from the rule date ---

def _pay(on, start, end, days, leave_type="Vacation", holidays=None, today=None, **pots):
    """Project one leave from its rule date, the way approval does.

    Args:
        on: The rule date.
        start: The first day.
        end: The last day.
        days: The days it costs.
        leave_type: The leave type.
        holidays: Holiday rows for a crossing request.
        today: The day it is decided; defaults to the rule date.
        **pots: Balance columns to start non-zero.

    Returns:
        The projected balances.
    """
    split = leave_day_split(start, end, days, on, holidays)
    carryover_days, waived_days = carryover_terms(split.carryover_days, on, today or on)
    return simulate_leave_impact(_zeroed(**pots), leave_type, days, carryover_days, waived_days)


def test_a_sick_day_this_year_comes_from_vacation_not_carry_over():
    # The case that started this: sick and Make-Up empty, vacation and carry-over both there.
    after = _pay(NOV_2026, NOV_2026, NOV_2026, 1, "Sick or Personal Day",
                 CurrentVacationBalance=5, CarryOver=3)
    assert after["CurrentVacationBalance"] == 4
    assert after["CarryOver"] == 3


def test_next_years_feb_comes_from_carry_over_from_a_nov_rule_date():
    after = _pay(NOV_2026, date(2027, 2, 1), date(2027, 2, 2), 2, CurrentVacationBalance=10, CarryOver=3)
    assert after["CarryOver"] == 1
    assert after["CurrentVacationBalance"] == 10


def test_feb_from_a_feb_rule_date_overflows_into_vacation():
    after = _pay(FEB_2027, FEB_2027, date(2027, 2, 12), 3, CurrentVacationBalance=15, CarryOver=2)
    assert after["CarryOver"] == 0
    assert after["CurrentVacationBalance"] == 14


def test_feb_from_a_feb_rule_date_with_no_carry_over_is_paid_by_vacation():
    after = _pay(FEB_2027, FEB_2027, FEB_2027, 1, CurrentVacationBalance=15)
    assert after["CarryOver"] == 0
    assert after["CurrentVacationBalance"] == 14


def test_a_nov_rule_date_still_pays_from_carry_over_when_approved_in_january():
    after = _pay(NOV_2026, date(2027, 2, 1), date(2027, 2, 2), 2, today=date(2027, 1, 5),
                 CurrentVacationBalance=10, CarryOver=3)
    assert after["CarryOver"] == 1
    assert after["CurrentVacationBalance"] == 10


def test_crossing_new_year_splits_vacation_and_carry_over():
    after = _pay(NOV_2026, date(2026, 12, 30), date(2027, 1, 4), 3, holidays=NEW_YEARS,
                 CurrentVacationBalance=5, CarryOver=1)
    assert after["CurrentVacationBalance"] == 3                     # Dec 30 and 31
    assert after["CarryOver"] == 0                                  # Jan 4


def test_crossing_new_year_with_make_up_and_no_carry_over():
    after = _pay(NOV_2026, date(2026, 12, 30), date(2027, 1, 4), 3, holidays=NEW_YEARS,
                 CurrentVacationBalance=5, CurrentOvertimeBalance=2)
    assert after["CurrentOvertimeBalance"] == 0                     # Make-Up pays first, for any date
    assert after["CurrentVacationBalance"] == 4
    assert after["CarryOver"] == 0                                  # never below zero


def test_crossing_the_end_of_march_splits_carry_over_and_vacation():
    after = _pay(MAR_2027, MAR_2027, date(2027, 4, 1), 3, CurrentVacationBalance=10, CarryOver=2)
    assert after["CarryOver"] == 0                                  # Mar 30 and 31
    assert after["CurrentVacationBalance"] == 9                     # Apr 1


def test_after_the_next_year_line_expired_current_year_days_pay():
    # Created Mar 30 for Mar 31, approved Apr 2 after the reset: vacation pays (#551).
    after = _pay(MAR_2027, date(2027, 3, 31), date(2027, 3, 31), 1, today=date(2027, 4, 2),
                 CurrentVacationBalance=10)
    assert after["CurrentVacationBalance"] == 9
    assert after["CarryOver"] == 0


def test_nothing_is_waived_after_expiry_any_more():
    # Mar 30 to Apr 1 approved Apr 2: every day comes from vacation (#551).
    after = _pay(MAR_2027, MAR_2027, date(2027, 4, 1), 3, today=date(2027, 4, 2), CurrentVacationBalance=10)
    assert after["CurrentVacationBalance"] == 7


def test_sick_and_make_up_still_pay_before_any_day_is_waived():
    after = _pay(MAR_2027, date(2027, 3, 31), date(2027, 3, 31), 1, "Sick or Personal Day",
                 today=date(2027, 4, 2), CurrentSickDayBalance=1, CurrentVacationBalance=10)
    assert after["CurrentSickDayBalance"] == 0
    assert after["CurrentVacationBalance"] == 10


# --- the rule date ---

def test_rule_date_is_the_edit_date_else_the_toronto_created_date():
    utc_evening = datetime(2026, 11, 5, 2, 30, tzinfo=timezone.utc)    # still Nov 4 in Toronto
    assert rule_date_for(SimpleNamespace(rules_on=None, created_at=utc_evening)) == date(2026, 11, 4)
    naive = datetime(2026, 11, 5, 2, 30)                               # SQLite hands back naive UTC
    assert rule_date_for(SimpleNamespace(rules_on=None, created_at=naive)) == date(2026, 11, 4)
    moved = SimpleNamespace(rules_on=date(2027, 1, 6), created_at=utc_evening)
    assert rule_date_for(moved) == date(2027, 1, 6)


def test_rule_date_from_fields_reads_the_same_values():
    today = date(2027, 5, 1)
    assert rule_date_from_fields({"RulesOn": "2027-01-06", "Created": "2026-11-05T02:30:00Z"}, today) == date(2027, 1, 6)
    assert rule_date_from_fields({"Created": "2026-11-05T02:30:00Z"}, today) == date(2026, 11, 4)
    assert rule_date_from_fields({}, today) == today                    # nothing stored: being created now
    assert rule_date_from_fields({"Created": "not a date"}, today) == today


# --- the cascade: carry-over never negative, and skipped past expiry ---

def test_carry_over_never_goes_negative_from_spending():
    # Carry Over 5, Make-Up 0, spend an 8-day vacation: Make-Up -> -8 overflows
    # into Carry Over, which pays its 5 down to 0 (never below) and passes the
    # rest to Vacation. Carry-over ends at 0, Vacation absorbs the shortfall.
    result = cascade_current_year_pure(sick=5, overtime=-8, carryover=5, vacation=10)
    assert result["CarryOver"] == 0
    assert result["CurrentOvertimeBalance"] == 0
    assert result["CurrentVacationBalance"] == 7                    # 10 - (8 - 5)


def test_cascade_skips_carry_over_for_dates_it_cannot_pay_for():
    # carryover_days 0: the Make-Up deficit goes straight to Vacation and carry-over
    # is left untouched.
    result = cascade_current_year_pure(
        sick=5, overtime=-3, carryover=5, vacation=10, carryover_days=0,
    )
    assert result["CarryOver"] == 5                                 # untouched
    assert result["CurrentVacationBalance"] == 7                    # took the whole 3 days


def test_simulate_leave_pays_from_carry_over_only_for_days_inside_its_dates():
    # A salaried person with carry-over, a 3-day vacation dated outside its period:
    # carry-over pays none of it.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 5,
           "CurrentOvertimeBalance": 0, "CurrentSickDayBalance": 5}
    # Apr-Dec case (today Oct 2026, a request in Apr 2027).
    apr_dec = simulate_leave_impact(emp, "Vacation", 3, carryover_days=0)
    assert apr_dec["CarryOver"] == 5 and apr_dec["CurrentVacationBalance"] == 7
    # Within the window, carry-over does pay.
    within = simulate_leave_impact(emp, "Vacation", 3, carryover_days=3)
    assert within["CarryOver"] == 2 and within["CurrentVacationBalance"] == 10


def test_jeffs_case_five_next_year_vacations_spend_vacation_not_carry_over():
    # Jeff: V = 11, C = 0 in Oct 2026, five 1-day vacations in Jan-Mar 2027.
    # Those dates are within the carry-over window, but C is 0, so each day comes
    # out of Vacation: 11 -> 6, and carry-over never goes negative.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 11, "CarryOver": 0,
           "CurrentOvertimeBalance": 0, "CurrentSickDayBalance": 0}
    for _ in range(5):
        projected = simulate_leave_impact(emp, "Vacation", 1, carryover_days=1)
        emp = {**emp, **projected}                                  # feed the result into the next day
    assert emp["CurrentVacationBalance"] == 6
    assert emp["CarryOver"] == 0


def test_next_year_vacation_spends_vacation_once_make_up_runs_out():
    # The old next-year path never touched Vacation; now it does. Make-Up 1, a
    # 3-day vacation: Make-Up 1 -> 0 covers one day, Vacation takes the other two.
    emp = {"SalaryHourly": "Salary", "CurrentVacationBalance": 10, "CarryOver": 0,
           "CurrentOvertimeBalance": 1, "CurrentSickDayBalance": 0}
    projected = simulate_leave_impact(emp, "Vacation", 3, carryover_days=3)
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


def test_a_capped_cascade_also_leaves_a_pre_existing_negative_in_place():
    result = cascade_current_year_pure(sick=0, overtime=-3, carryover=-5, vacation=10, carryover_days=1)
    assert result["CarryOver"] == -5
    assert result["CurrentVacationBalance"] == 7


def test_cascade_moves_only_the_request_created_carry_over_deficit():
    # A pre-existing Carry Over -5, and a spend drives Make-Up to -3 which overflows
    # into carry-over (-5 -> -8). Only the 3 days THIS request created move onto
    # vacation; the original -5 stays for the setup fix. Carry-over ends at -5,
    # vacation drops by 3 (#407 review).
    result = cascade_current_year_pure(sick=0, overtime=-3, carryover=-5, vacation=10)
    assert result["CarryOver"] == -5                               # original negative left in place
    assert result["CurrentOvertimeBalance"] == 0
    assert result["CurrentVacationBalance"] == 7                   # 10 - 3


# --- what is refused, from the rule date ---

NOV_5 = date(2026, 11, 5)


def _refusal(on, start, end, days, leave_type="Vacation", **pots):
    """The refusal for one request, from its rule date and balances."""
    split = leave_day_split(start, end, days, on)
    return booking_refusal(_zeroed(**pots), leave_type, split, on)


def test_refused_two_carry_over_days_cover_two_days_next_february():
    assert _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 2), 2, CarryOver=2) is None


def test_current_year_days_can_pay_for_next_february_too():
    # They carry at year end on their own, so 2 next-year days plus 15 cover 3 (#551).
    assert _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 3), 3, CarryOver=2, CurrentVacationBalance=15) is None


def test_refused_three_days_next_february_with_two_days_in_all():
    message = _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 3), 3, CarryOver=2, CurrentVacationBalance=0)
    assert message == "These dates need 3 days and you have 2 for 2027."


def test_refused_april_next_year_whatever_the_balances():
    message = _refusal(NOV_5, date(2027, 4, 5), date(2027, 4, 5), 1, CarryOver=9, CurrentVacationBalance=15)
    assert message == "Leave dated after March 31, 2027 can be booked from January 1, 2027."


def test_make_up_alone_can_pay_for_next_february():
    assert _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 2), 2, CurrentOvertimeBalance=2) is None


def test_vacation_below_zero_counts_as_nothing_for_next_year():
    message = _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 1), 1, CurrentVacationBalance=-2)
    assert message == "These dates need 1 day and you have 0 for 2027."


def test_make_up_counts_toward_next_year_dates():
    message = _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 3), 3, CarryOver=1, CurrentOvertimeBalance=1)
    assert message == "These dates need 3 days and you have 2 for 2027."


def test_a_negative_pot_counts_as_nothing_when_refusing():
    message = _refusal(NOV_5, date(2027, 2, 1), date(2027, 2, 1), 1, CarryOver=-3, CurrentOvertimeBalance=0.5)
    assert "you have 0.5" in message


def test_this_years_dates_are_allowed_at_zero_vacation():
    assert _refusal(NOV_5, date(2026, 12, 1), date(2026, 12, 1), 1) is None


def test_a_late_request_dated_last_year_is_not_refused():
    assert _refusal(NOV_5, date(2025, 8, 4), date(2025, 8, 4), 1) is None


def test_from_a_feb_rule_date_this_february_is_allowed_with_no_carry_over():
    assert _refusal(date(2027, 2, 10), date(2027, 2, 15), date(2027, 2, 15), 1) is None


def test_from_a_feb_rule_date_next_february_is_refused():
    message = _refusal(date(2027, 2, 10), date(2028, 2, 15), date(2028, 2, 15), 1, CarryOver=5)
    assert message == "Leave dated after December 31, 2027 can be booked from January 1, 2028."


def test_sick_and_hourly_are_never_refused():
    assert _refusal(NOV_5, date(2027, 6, 1), date(2027, 6, 1), 1, "Sick or Personal Day") is None
    hourly = {"SalaryHourly": "Hourly", "CarryOver": 0, "CurrentOvertimeBalance": 0}
    split = leave_day_split(date(2030, 1, 1), date(2030, 1, 1), 1, NOV_5)
    assert booking_refusal(hourly, "Vacation", split, NOV_5) is None


def test_no_dates_are_never_refused():
    assert booking_refusal(_zeroed(), "Vacation", leave_day_split(None, None, 2, NOV_5), NOV_5) is None


def test_bookable_until_from_april_on_opens_next_march_only_with_something_to_pay():
    assert bookable_until(_zeroed(), NOV_5) == date(2026, 12, 31)
    assert bookable_until(_zeroed(CarryOver=1), NOV_5) == date(2027, 3, 31)
    assert bookable_until(_zeroed(CurrentOvertimeBalance=1), NOV_5) == date(2027, 3, 31)


def test_bookable_until_from_jan_to_mar_is_the_end_of_this_year():
    assert bookable_until(_zeroed(CarryOver=5), date(2027, 2, 10)) == date(2027, 12, 31)


def test_a_form_this_year_reads_no_holidays(monkeypatch):
    async def boom(_province):
        raise AssertionError("no holiday read expected")

    monkeypatch.setattr("app.services.balance.get_holidays_for_province", boom)
    refusal = run(booking_refusal_for(_zeroed(), "Vacation", date(2026, 12, 1), date(2026, 12, 2), None, NOV_5))
    assert refusal is None


def test_a_form_works_out_its_days_to_compare_them(monkeypatch):
    async def no_holidays(_province):
        return []

    monkeypatch.setattr("app.services.balance.map_location_to_province", lambda _location: "ON")
    monkeypatch.setattr("app.services.balance.get_holidays_for_province", no_holidays)
    pots = _zeroed(CarryOver=2, Location="Ontario")
    # Mon to Wed is 3 working days, and 2 carried days cannot cover them.
    assert "need 3 days" in run(booking_refusal_for(pots, "Vacation", date(2027, 2, 1), date(2027, 2, 3), None, NOV_5))
    # The same day as a 4 hour part day needs half a day.
    part = run(booking_refusal_for(pots, "Half Day or Partial Day Off", date(2027, 2, 1), date(2027, 2, 1), 4, NOV_5))
    assert part is None


# --- submission refuses a past-limit request before any row is created ---

def _salaried(**over):
    """A salaried staff record with balances, for submit_request."""
    base = {"Title": "Pat Staff", "EmailAddress": "pat@ucsh.com", "SalaryHourly": "Salary",
            "CurrentVacationBalance": 10, "CarryOver": 0}
    base.update(over)
    return {"id": "7", "fields": base}


def test_submit_refuses_a_vacation_past_the_limit_and_creates_nothing(monkeypatch):
    # A June-next-year vacation cannot be paid for yet, whatever the balances.
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
    assert "can be booked from January 1" in str(exc.value)
    assert created == []                                            # refused before the SharePoint write


def test_submit_allows_a_vacation_within_the_limit(monkeypatch):
    created = []

    async def create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", create)

    # A leave submission now reads the leave list for an overlap clash (#446);
    # an empty list means no clash, so only the booking limit is under test here.
    async def no_leave(*a, **k):
        return []

    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", no_leave)
    today = date.today()
    form = parse_request_form("leave", {
        "leave_type": "Vacation", "start_date": today.isoformat(), "end_date": today.isoformat(),
    })
    item = run(submit_request("leave", form, _salaried(), "request_page"))
    assert item["id"] == "99" and len(created) == 1                 # it was created
