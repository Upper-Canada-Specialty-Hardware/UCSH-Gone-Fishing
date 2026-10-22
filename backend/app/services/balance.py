import logging
from datetime import date

from app.config import settings
from app.services.app_settings import toronto_today  # Toronto calendar day for the booking policy (#407)
from app.services.employee import get_employee_by_id
from app.services.employment import NO_COST_LEAVE, is_hourly, leave_costs_balance, whole_sick_days
from app.repositories.staff_store import staff_store  # Staff Directory reads/writes follow staff_storage

logger = logging.getLogger(__name__)

# The two leave types the vacation booking limit governs; salaried staff only
# (hourly staff have no vacation balance and are never limited here) (#407).
LEAVE_BOOKING_TYPES = ("Vacation", "Half Day or Partial Day Off")


# --- Pure math functions (no SharePoint I/O) ---

def cascade_current_year_pure(
    sick: float, overtime: float, carryover: float, vacation: float,
    carryover_available: bool = True,
) -> dict:
    """Sick -> Make-Up -> (Carry Over) -> Vacation cascade as pure math.

    A deficit in a pot overflows into the next pot down the chain. Spending never
    drives carry-over MORE negative than it started: only a deficit this request
    itself creates is pushed on to Vacation, while a pre-existing negative
    carry-over is left in place (moving it here would silently take vacation days
    with no reason recorded; clearing a stored negative is the employee-setup
    fix's job, which records why). When ``carryover_available`` is False (the
    request runs past the carry-over expiry, #407), the Make-Up deficit skips
    carry-over and lands on Vacation instead, leaving carry-over untouched.
    Vacation is the final backstop and may go below zero. There is no separate
    next-year path any more: next-year dates use these same rules (#407).

    Args:
        sick: Sick pot (days).
        overtime: Make-Up / overtime pot (days).
        carryover: Carry-over pot (days).
        vacation: Vacation pot (days).
        carryover_available: Whether carry-over may pay this request; False skips
            the carry-over step so the deficit goes straight to Vacation.

    Returns:
        The four pots after the cascade, keyed by their SharePoint column names.
    """
    carryover_floor = min(carryover, 0.0)             # pre-existing negative stays; only this request's deficit moves
    for _ in range(60):
        if sick < 0:                                  # sick overdrawn: push the deficit to Make-Up
            overtime += sick
            sick = 0
            continue
        if overtime < 0:                              # Make-Up overdrawn: where the deficit goes next
            if carryover_available:                   # carry-over may pay: it absorbs, overflow handled below
                carryover += overtime
            else:                                     # expired for these dates: skip carry-over, hit Vacation
                vacation += overtime
            overtime = 0
            continue
        if carryover < carryover_floor:               # overdrawn BELOW where it started: only that part moves
            vacation += carryover - carryover_floor   # the request-created shortfall lands on Vacation
            carryover = carryover_floor               # leave any pre-existing negative for the setup fix
            continue
        break
    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": sick,
        "CarryOver": carryover,
        "CurrentOvertimeBalance": overtime,
    }


def booking_limit(vacation: float, carryover: float, today: date) -> date:
    """The last day a person may book vacation to, from their V and C (#407).

    Pure, and the one place the booking-limit rule lives, so submission,
    approval, the preview and the stored "Can book vacation until" date can never
    drift apart. A negative vacation counts as zero, and carry-over at or below
    zero counts as zero (carry-over is never negative after #407).

    Args:
        vacation: The vacation pot (days).
        carryover: The carry-over pot (days).
        today: Today in Toronto (the calendar "this year" means).

    Returns:
        Dec 31 this year (V==0, C>0), Mar 31 next year (V>0, any C), or Dec 31
        next year (V==0, C==0).
    """
    v = vacation if vacation > 0 else 0.0             # V<0 treated as 0 for this rule
    c = carryover if carryover > 0 else 0.0           # C<=0 treated as 0 (never negative)
    if v == 0 and c > 0:
        return date(today.year, 12, 31)               # only carry-over left: until the end of this year
    if v == 0 and c == 0:
        return date(today.year + 1, 12, 31)           # nothing left: next year is wide open
    return date(today.year + 1, 3, 31)                # vacation left: through next year's carry-over window


def carryover_expiry(today: date) -> date:
    """When today's carry-over expires: Mar 31 this year while ahead, else next year (#407).

    Args:
        today: Today in Toronto.

    Returns:
        March 31 of this year when that date has not yet passed, otherwise March
        31 of next year.
    """
    this_year = date(today.year, 3, 31)               # this year's carry-over deadline
    return this_year if today <= this_year else date(today.year + 1, 3, 31)


def carryover_available_for(last_day: date | None, today: date) -> bool:
    """Whether carry-over may pay a request whose last day is ``last_day`` (#407).

    Carry-over pays none of a request if any day of it is after the carry-over
    expiry; the last day is the latest day, so it is the one that decides.

    Args:
        last_day: The request's last day, or None when it has no dates.
        today: Today in Toronto.

    Returns:
        True when the whole request falls on or before the carry-over expiry.
    """
    if last_day is None:                              # no dates: nothing to compare, allow it
        return True
    return last_day <= carryover_expiry(today)


def booking_limit_message(limit: date) -> str:
    """The plain refusal naming the limit, e.g. 'You can book vacation until March 31, 2027.' (#407).

    Args:
        limit: The booking limit date.

    Returns:
        The sentence shown to the person, with a portable month name (no %-d).
    """
    return f"You can book vacation until {limit:%B} {limit.day}, {limit.year}."


def booking_refusal(
    sp_fields: dict, leave_type: str, last_day: date | None, today: date,
) -> str | None:
    """The refusal when a salaried Vacation/part-day request runs past the limit (#407).

    The limit is computed from the person's CURRENT vacation and carry-over, so
    the same balances the deduction would read decide whether the dates are
    allowed. Hourly staff and every other leave type are never limited.

    Args:
        sp_fields: The person's Staff-Directory-shaped balances (and SalaryHourly).
        leave_type: The request's leave type.
        last_day: The request's last day, or None.
        today: Today in Toronto.

    Returns:
        The refusal sentence when the last day is past the limit, else None.
    """
    if is_hourly(sp_fields):                          # hourly: no vacation balance, never limited
        return None
    if leave_type not in LEAVE_BOOKING_TYPES:         # only vacation and part days are booked
        return None
    if last_day is None:                              # no last day: nothing to check
        return None
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    limit = booking_limit(vacation, carryover, today)
    if last_day > limit:                              # the request reaches past what they may book
        return booking_limit_message(limit)
    return None


def apply_vacation_offset_pure(
    vacation: float, overtime: float,
) -> tuple[float, float]:
    """If vacation negative and overtime positive, offset. Returns (vacation, overtime)."""
    if vacation >= 0 or overtime <= 0:
        return vacation, overtime
    new_vacation = vacation + overtime
    new_overtime = 0.0
    if new_vacation > 0:
        new_overtime = new_vacation
        new_vacation = 0.0
    return new_vacation, new_overtime


# --- Async functions (SP reads/writes, using pure math internally) ---

async def cascade_current_year(
    employee_id: str | int, carryover_available: bool = True,
) -> dict:
    """Run the Sick -> Make-Up -> (Carry Over) -> Vacation cascade on SharePoint.

    Reads the employee's pots, applies ``cascade_current_year_pure`` (so the
    carry-over step is skipped when ``carryover_available`` is False, #407), and
    writes the result back, looping until it settles.

    Args:
        employee_id: Staff Directory item id.
        carryover_available: Whether carry-over may pay this request (False when
            it runs past the carry-over expiry).

    Returns:
        The final balances, keyed by their SharePoint column names.
    """
    result = {}
    for iteration in range(60):
        emp = await get_employee_by_id(employee_id)
        fields = emp["fields"]
        sick = float(fields.get("CurrentSickDayBalance", 0) or 0)
        overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)
        carryover = float(fields.get("CarryOver", 0) or 0)
        vacation = float(fields.get("CurrentVacationBalance", 0) or 0)

        result = cascade_current_year_pure(sick, overtime, carryover, vacation, carryover_available)

        if (result["CurrentSickDayBalance"] == sick
                and result["CurrentOvertimeBalance"] == overtime
                and result["CarryOver"] == carryover
                and result["CurrentVacationBalance"] == vacation):
            break

        await staff_store.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id, result,
        )

    return result


def is_next_year_request(start_date: date, end_date: date) -> bool:
    """Whether a request falls in next calendar year (email wording, hourly counter).

    This no longer picks a different cascade (removed in #407); it only decides
    the next-year email wording and the hourly vacation-taken counter's year.

    Args:
        start_date: The request's first day.
        end_date: The request's last day.

    Returns:
        True when the start or end date is next calendar year.
    """
    current_year = date.today().year
    return start_date.year == current_year + 1 or end_date.year == current_year + 1


async def recalculate_request_allow_date(
    employee_id: str | int, vacation: float, carryover: float
) -> str | None:
    """Recompute and store the can-book-vacation date from V and C (#407).

    The date is the booking limit from ``booking_limit`` (the single rule), using
    today in Toronto, and is ALWAYS computed (even when both pots are non-zero,
    which earlier left the stored date unchanged). The write still happens only
    when the stored date actually differs.

    Args:
        employee_id: Staff Directory item id of the person.
        vacation: Current vacation balance (days).
        carryover: Current carry-over balance (days).

    Returns:
        The new date as a ``YYYY-MM-DD`` string when it was written, or None when
        the stored date already matched.
    """
    new_date = booking_limit(vacation, carryover, toronto_today())  # the one booking rule

    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    current_rad = fields.get("RequestAllowDate")

    new_date_str = new_date.isoformat()
    if current_rad and current_rad[:10] == new_date_str:
        return None  # No change needed

    # Include Title to avoid clearing it (SP PatchItem behavior)
    await staff_store.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY,
        employee_id,
        {
            "RequestAllowDate": new_date_str,
            "Title": fields.get("Title", ""),
        },
    )
    logger.info("Updated RequestAllowDate for employee %s to %s", employee_id, new_date_str)
    return new_date_str  # the caller records this as its own history row


async def apply_vacation_offset(employee_id: str | int) -> None:
    """After overtime approval: if vacation negative and overtime positive, offset."""
    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    vacation = float(fields.get("CurrentVacationBalance", 0) or 0)
    overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)

    new_vacation, new_overtime = apply_vacation_offset_pure(vacation, overtime)

    if new_vacation == vacation and new_overtime == overtime:
        return

    await staff_store.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, employee_id,
        {"CurrentVacationBalance": new_vacation, "CurrentOvertimeBalance": new_overtime},
    )


# --- Simulation functions (pure math, for projected balance display) ---

def simulate_leave_impact(
    sp_fields: dict, leave_type: str, days: float, carryover_available: bool = True,
) -> dict | None:
    """Simulate balance impact of approving a leave request.

    Returns projected SP-style balance dict, or None for no-impact types. Spends
    Make-Up first (or Sick for a sick/personal day), then cascades the shortfall
    down Sick -> Make-Up -> Carry Over -> Vacation. When ``carryover_available``
    is False (the request runs past the carry-over expiry, #407), the cascade
    skips carry-over. Next-year dates use these same rules (no separate path).

    Args:
        sp_fields: The person's Staff-Directory-shaped balances (and SalaryHourly).
        leave_type: The request's leave type.
        days: The days the leave costs.
        carryover_available: Whether carry-over may pay this request.

    Returns:
        The projected balances, or None when nothing moves for this person/type.
    """
    if leave_type in NO_COST_LEAVE:      # bereavement, jury duty, work from home (#375)
        return None                       # no impact: and no overflow step either

    # hourly (#300): sick is their only balance. A sick or part day takes
    # whole sick days and stops there (no cascade, so sick may go below zero);
    # vacation is only counted, so it moves nothing.
    if is_hourly(sp_fields):
        if not leave_costs_balance(sp_fields, leave_type):    # vacation etc.: counted only
            return None
        return {
            "CurrentVacationBalance": float(sp_fields.get("CurrentVacationBalance", 0) or 0),  # untouched
            "CurrentSickDayBalance": float(sp_fields.get("CurrentSickDayBalance", 0) or 0) - whole_sick_days(days),
            "CurrentOvertimeBalance": float(sp_fields.get("CurrentOvertimeBalance", 0) or 0),  # untouched
            "CarryOver": float(sp_fields.get("CarryOver", 0) or 0),                            # untouched
        }

    sick = float(sp_fields.get("CurrentSickDayBalance", 0) or 0)
    overtime = float(sp_fields.get("CurrentOvertimeBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)

    if leave_type in ("Vacation", "Half Day or Partial Day Off"):
        overtime -= days                  # vacation and part days come off Make-Up first
    elif leave_type == "Sick or Personal Day":
        sick -= days                      # sick/personal comes off Sick first

    # One cascade for every date (no separate next-year path, #407); carry-over
    # is skipped when it has expired for these dates.
    result = cascade_current_year_pure(sick, overtime, carryover, vacation, carryover_available)
    sick = result["CurrentSickDayBalance"]
    overtime = result["CurrentOvertimeBalance"]
    carryover = result["CarryOver"]
    vacation = result["CurrentVacationBalance"]

    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": sick,
        "CurrentOvertimeBalance": overtime,
        "CarryOver": carryover,
    }


def simulate_overtime_impact(sp_fields: dict, hours: float) -> dict:
    """Simulate balance impact of approving an overtime request."""
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)
    overtime = float(sp_fields.get("CurrentOvertimeBalance", 0) or 0)

    overtime += hours / 8
    vacation, overtime = apply_vacation_offset_pure(vacation, overtime)

    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": float(sp_fields.get("CurrentSickDayBalance", 0) or 0),
        "CurrentOvertimeBalance": overtime,
        "CarryOver": float(sp_fields.get("CarryOver", 0) or 0),
    }


def simulate_carryover_payout_impact(
    sp_fields: dict, days: float, request_type: str,
) -> dict | None:
    """Simulate balance impact of approving a carryover/payout request.
    Returns None if vacation would go negative (system override reject).
    """
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    payout = float(sp_fields.get("Payout", 0) or 0)

    final_vacation = vacation - days
    if final_vacation < 0:
        return None

    if request_type == "Carry Over":
        carryover += days
    else:
        payout += days

    return {
        "CurrentVacationBalance": final_vacation,
        "CurrentSickDayBalance": float(sp_fields.get("CurrentSickDayBalance", 0) or 0),
        "CurrentOvertimeBalance": float(sp_fields.get("CurrentOvertimeBalance", 0) or 0),
        "CarryOver": carryover,
        "Payout": payout,
    }
