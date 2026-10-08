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

from app.services.balance import (
    is_next_year_request,
    simulate_carryover_payout_impact,
    simulate_leave_impact,
    simulate_overtime_impact,
)
from app.services.business_days import calculate_business_days
from app.services.employee import map_location_to_province
from app.services.holidays import (
    get_half_friday_season,
    get_holidays_for_province,
    is_company_holiday,
    is_half_friday,
)
from app.services.request_intake import HOURS_PER_DAY, PARTIAL_DAY

logger = logging.getLogger(__name__)

NO_COST_LEAVE = ("Bereavement", "Jury Duty")   # simulate_leave_impact returns None for these
SICK = "Sick or Personal Day"                  # the one kind hourly staff still spend a balance on


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


def build_preview(request_type: str, form: BaseModel, emp_fields: dict, holidays: list[dict]) -> dict:
    """Work out what approving this form would do, without any I/O.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        form: The checked form from request_intake.parse_request_form.
        emp_fields: The employee's Staff Directory fields (balances, SalaryHourly).
        holidays: The employee's province's holiday rows ([] when unknown).

    Returns:
        {"days": float, "projected": SharePoint-style balances or None,
        "unchanged": a sentence when no balance changes, else "",
        "notes": [sentences worth knowing], "warning": a sentence when the
        request would be rejected automatically, else "", "next_year": bool}.
    """
    season = get_half_friday_season(holidays)              # (start, end) or (None, None)
    notes: list[str] = []
    warning = ""
    projected = None
    next_year = False

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
        projected = simulate_leave_impact(emp_fields, leave_type, days, next_year)
        if next_year and projected is not None:
            notes.append("Next year's time off uses Make-Up, then Carry Over. Next year's vacation is not touched.")
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
        if projected is None:                              # vacation would go below zero
            have = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
            warning = f"You have {have:g} vacation days, fewer than {days:g}. This request would be rejected."

    unchanged = ""
    # Hourly staff keep their balances, except for sick days (same rule as the manager's view).
    hourly_kept = emp_fields.get("SalaryHourly") == "Hourly" and not (request_type == "leave" and form.leave_type == SICK)
    if hourly_kept:
        projected = None
        unchanged = "You are paid hourly, so this does not change a balance."
    elif request_type == "leave" and form.leave_type in NO_COST_LEAVE:
        unchanged = "Bereavement and jury duty do not use any balance."

    return {
        "days": days,
        "projected": projected,
        "unchanged": unchanged,
        "notes": notes,
        "warning": warning,
        "next_year": next_year,
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
