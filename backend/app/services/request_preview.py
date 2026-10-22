"""What a request would do to the employee's balances, before it is sent.

The request page shows this under the form: the working days the request
counts, each balance now and after approval, and any rule that would reject
it automatically. Nothing is written. It uses the same pieces approval uses
(calculate_business_days and the simulate_* functions in services/balance.py),
so the preview and the real deduction cannot drift apart.

Split like the balance engine: ``build_preview`` is pure (no I/O, easy to
test), ``preview_request`` loads the holidays and calls it.
"""

import logging
from datetime import date, timedelta

from pydantic import BaseModel

from app.services.app_settings import toronto_today  # Toronto calendar day for the booking policy (#407)
from app.services.balance import (
    LEAVE_BOOKING_TYPES,
    booking_limit,
    booking_limit_message,
    carryover_available_for,
    is_next_year_request,
    simulate_carryover_payout_impact,
    simulate_leave_impact,
    simulate_overtime_impact,
)
from app.services.business_days import calculate_business_days
from app.services.employee import map_location_to_province
from app.services.employment import (
    NO_COST_LEAVE,
    PARTIAL_DAY as PART_DAY_LEAVE,
    VACATION,
    floor_warning,
    is_hourly,
    leave_costs_balance,
    overtime_costs_balance,
)
from app.services.holidays import (
    get_half_friday_season,
    get_holidays_for_province,
    is_company_holiday,
    is_half_friday,
)
from app.services.request_intake import HOURS_PER_DAY, PARTIAL_DAY

logger = logging.getLogger(__name__)

# The "uses no balance" sentence for each no-cost leave type, worded per type so
# work from home does not read as if it were bereavement (#388). Any type in
# employment.NO_COST_LEAVE not listed here falls back to the generic sentence.
NO_COST_LEAVE_NOTE = {
    "Bereavement": "Bereavement does not use any balance.",
    "Jury Duty": "Jury duty does not use any balance.",
    "Work from Home": "Work from home does not use any balance.",
}


def _holidays_in(start: date, end: date, holidays: list[dict]) -> list[str]:
    """Names of company holidays on weekdays between two dates, inclusive.

    Args:
        start: First day.
        end: Last day.
        holidays: The province's holiday rows.

    Returns:
        Holiday names in date order; weekends are skipped (they are never counted anyway).
    """
    names = []
    day = start
    for _ in range(366):                                   # same guard idea as calculate_business_days
        if day > end:
            break
        if day.weekday() < 5:                              # Monday to Friday only
            hit, name = is_company_holiday(day, holidays)
            if hit:
                names.append(name)
        day += timedelta(days=1)
    return names


def build_preview(
    request_type: str, form: BaseModel, emp_fields: dict, holidays: list[dict],
    today: date | None = None,
) -> dict:
    """Work out what approving this form would do, without any I/O.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        form: The checked form from request_intake.parse_request_form.
        emp_fields: The employee's Staff Directory fields (balances, SalaryHourly).
        holidays: The employee's province's holiday rows ([] when unknown).
        today: Today in Toronto (the booking policy's "this year"); defaults to it.

    Returns:
        {"days": float, "projected": SharePoint-style balances or None,
        "unchanged": a sentence when no balance changes, else "",
        "notes": [sentences worth knowing], "warning": a sentence when the
        request would be rejected automatically, else "", "next_year": bool,
        "below_zero": ``employment.floor_warning`` when approving would take the
        floor below zero (shown in red; the manager may still approve), else None,
        "booking_limit": {"date", "refused", "message"} for a salaried
        Vacation/part-day request, else None (#407)}.
    """
    today = today or toronto_today()                       # the booking policy's calendar day
    season = get_half_friday_season(holidays)              # (start, end) or (None, None)
    notes: list[str] = []
    warning = ""
    projected = None
    next_year = False
    booking_limit_info = None                              # set for salaried vacation / part days (#407)

    if request_type == "leave":
        leave_type = form.leave_type
        if leave_type == PARTIAL_DAY:
            days = form.partial_hours / HOURS_PER_DAY      # hours to days, as submission stores it
            hit, name = is_company_holiday(form.start_date, holidays)
            if hit:
                warning = f"{form.start_date} is the company holiday {name}. A part day on a holiday is rejected automatically."
            elif is_half_friday(form.start_date, season) and days > 0.5:
                warning = f"{form.start_date} is a half-day Friday, so a part day can be at most 4 hours. Longer is rejected automatically."
        else:
            days = calculate_business_days(form.start_date, form.end_date, holidays, season)
            skipped = _holidays_in(form.start_date, form.end_date, holidays)
            if skipped:
                notes.append(f"{', '.join(skipped)} {'is a company holiday' if len(skipped) == 1 else 'are company holidays'} and not counted.")
            if days <= 0:
                warning = "These dates have no working days."
        next_year = is_next_year_request(form.start_date, form.end_date)
        bc = is_hourly(emp_fields)                      # hourly: sick only, no cascade
        last_day = form.end_date                        # set for both a part day and a range
        carryover_avail = carryover_available_for(last_day, today)   # carry-over expired for these dates? (#407)
        projected = simulate_leave_impact(emp_fields, leave_type, days, carryover_avail)
        # Only salaried vacation and part days are held to the booking limit;
        # carry the limit and whether these dates are past it to the page (#407).
        if not bc and leave_type in LEAVE_BOOKING_TYPES:
            limit = booking_limit(
                float(emp_fields.get("CurrentVacationBalance", 0) or 0),
                float(emp_fields.get("CarryOver", 0) or 0),
                today,
            )
            refused = last_day is not None and last_day > limit
            booking_limit_info = {
                "date": limit.isoformat(),
                "refused": refused,
                "message": booking_limit_message(limit) if refused else "",
            }
        # Carry-over that cannot be used for these dates is worth saying when the
        # person actually has some (#407).
        if (not bc and not carryover_avail
                and float(emp_fields.get("CarryOver", 0) or 0) > 0):
            notes.append("Carry-over cannot be used for these dates, so Make-Up then Vacation are used.")
        if bc and leave_type == PART_DAY_LEAVE:
            notes.append("A part day uses one whole sick day.")
    elif request_type == "overtime":
        days = form.hours / HOURS_PER_DAY                  # Make-Up is kept in days
        hit, name = is_company_holiday(form.date, holidays)
        if hit:
            warning = f"{form.date} is the company holiday {name}. Overtime on a holiday is rejected automatically."
        projected = simulate_overtime_impact(emp_fields, form.hours)
        if float(emp_fields.get("CurrentVacationBalance", 0) or 0) < 0:
            notes.append("Your Vacation is below zero, so the Make-Up pays it back first.")
    else:
        days = form.days
        projected = simulate_carryover_payout_impact(emp_fields, days, form.type_of_request)
        if is_hourly(emp_fields):                       # no vacation balance to move
            projected = None
            warning = "Hourly staff have no vacation balance to carry over or pay out."
        elif projected is None:                            # vacation would go below zero
            have = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
            warning = f"You have {have:g} vacation days, fewer than {days:g}. This request would be rejected."

    unchanged = ""
    # Which kinds move a balance for this person (salaried or
    # hourly) is decided in services/employment.py, same as the manager's view.
    if request_type == "leave" and form.leave_type in NO_COST_LEAVE:
        projected = None                                   # nobody's balance moves for these
        unchanged = NO_COST_LEAVE_NOTE.get(form.leave_type, "This leave does not use any balance.")
    elif request_type == "leave" and is_hourly(emp_fields) and form.leave_type == VACATION:
        projected = None                                   # counted, not spent
        unchanged = "Counted only. No balance is used."
    elif (request_type == "leave" and not leave_costs_balance(emp_fields, form.leave_type)) or (
        request_type == "overtime" and not overtime_costs_balance(emp_fields)
    ):
        projected = None
        unchanged = "You are paid hourly, so this does not change a balance."

    return {
        "days": days,
        "projected": projected,
        "unchanged": unchanged,
        "notes": notes,
        "warning": warning,
        "next_year": next_year,
        "below_zero": floor_warning(emp_fields, projected),   # red, never a block
        "booking_limit": booking_limit_info,                  # the vacation booking limit (#407)
    }


async def preview_request(request_type: str, form: BaseModel, emp_fields: dict) -> dict:
    """Load the employee's holidays, then build the preview.

    A location with no province, or a holiday list that cannot be read, still
    gives a preview: without holidays, and with a note saying so.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        form: The checked form.
        emp_fields: The employee's Staff Directory fields.

    Returns:
        See build_preview.
    """
    try:
        holidays = await get_holidays_for_province(map_location_to_province(emp_fields.get("Location", "")))
        checked = True
    except Exception:  # noqa: BLE001 - an unknown location or a read failure only weakens the preview
        logger.warning("Request preview: holidays could not be loaded for %r", emp_fields.get("Location"), exc_info=True)
        holidays, checked = [], False
    result = build_preview(request_type, form, emp_fields, holidays)
    if not checked:
        result["notes"].append("Company holidays could not be checked, so they are not taken into account here.")
    return result
