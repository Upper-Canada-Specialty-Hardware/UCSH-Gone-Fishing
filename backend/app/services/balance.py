import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.config import settings
from app.services.app_settings import TORONTO, toronto_today  # the company's calendar day
from app.services.business_days import calculate_business_days
from app.services.employee import get_employee_by_id, map_location_to_province
from app.services.employment import NO_COST_LEAVE, is_hourly, leave_costs_balance, whole_sick_days
from app.services.holidays import get_half_friday_season, get_holidays_for_province
from app.repositories.staff_store import staff_store  # Staff Directory reads/writes follow staff_storage

logger = logging.getLogger(__name__)

# The two leave types that can be refused for their dates; salaried staff only
# (hourly staff have no vacation balance and are never refused here).
LEAVE_BOOKING_TYPES = ("Vacation", "Half Day or Partial Day Off")


# --- Pure math functions (no SharePoint I/O) ---

def cascade_current_year_pure(
    sick: float, overtime: float, carryover: float, vacation: float,
    carryover_days: float | None = None, waived_days: float = 0.0,
) -> dict:
    """Sick -> Make-Up -> Carry Over -> Vacation cascade as pure math.

    A deficit in a pot overflows into the next pot down the chain. A Make-Up
    deficit first waives up to ``waived_days`` (days due from a carry-over that has
    since expired, charged to nothing), then carry-over absorbs up to
    ``carryover_days``, and the rest lands on Vacation. Spending never drives
    carry-over more negative than it started: a deficit this request creates moves
    on to Vacation, while a pre-existing negative stays put for the employee-setup
    fix, which records why. Vacation is the final backstop and may go below zero.

    Args:
        sick: Sick pot (days).
        overtime: Make-Up / overtime pot (days).
        carryover: Carry-over pot (days).
        vacation: Vacation pot (days).
        carryover_days: The most carry-over may pay, the days dated inside its
            period; None means no cap (a refund or anything that is not leave).
        waived_days: The days charged to nothing because their carry-over expired.

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
            deficit = -overtime
            waived = min(deficit, waived_days)        # expired carry-over days are charged to nothing
            waived_days -= waived                     # a waived day is only waived once
            deficit -= waived
            # Carry-over only pays for days dated inside its period; None is uncapped.
            to_carryover = deficit if carryover_days is None else min(deficit, carryover_days)
            carryover -= to_carryover                 # any overdraft is handled just below
            vacation -= deficit - to_carryover        # days carry-over cannot pay land on Vacation
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


def carryover_period(on: date) -> tuple[date, date]:
    """The leave dates carry-over can pay for, seen from one rule date.

    Carried days are for the year after they were saved and are gone after Mar 31.
    A rule date from April on looks at next year's Jan 1 to Mar 31; a rule date
    from Jan to Mar looks at this year's.

    Args:
        on: The rule date.

    Returns:
        ``(Jan 1, Mar 31)`` of the carry-over's expiry year.
    """
    expiry = carryover_expiry(on)                     # Mar 31 this year or next
    return date(expiry.year, 1, 1), expiry


@dataclass(frozen=True)
class LeaveSplit:
    """How one request's days fall against what each pot can pay for.

    Attributes:
        carryover_days: Days dated inside the carry-over period.
        ahead_days: The part of those after Dec 31 of the rule date's year, which
            vacation cannot pay for.
        unbookable_days: Days after Dec 31 of the rule date's year and outside the
            carry-over period, which nothing can pay for yet.
    """

    carryover_days: float
    ahead_days: float
    unbookable_days: float


def _unbookable_from(on: date) -> date:
    """The first date nothing can pay for yet, seen from one rule date.

    Args:
        on: The rule date.

    Returns:
        The day after both Dec 31 of the rule date's year and the carry-over period.
    """
    _, period_end = carryover_period(on)
    return max(date(on.year, 12, 31), period_end) + timedelta(days=1)


def _overlap(first: date, last: date, low: date, high: date) -> tuple[date, date] | None:
    """The part of ``first..last`` inside ``low..high``.

    Args:
        first: The request's first day.
        last: The request's last day.
        low: The range's first day.
        high: The range's last day.

    Returns:
        The overlapping ``(first, last)``, or None when they do not meet.
    """
    start, end = max(first, low), min(last, high)     # the tighter of each end
    return (start, end) if start <= end else None


def leave_crosses_boundary(start: date | None, end: date | None, on: date) -> bool:
    """Whether a request is only partly inside one of the rule date's date ranges.

    Only such a request needs working days counted, and so company holidays read;
    a request wholly on one side takes its stored days as they are.

    Args:
        start: The request's first day, or None.
        end: The request's last day, or None.
        on: The rule date.

    Returns:
        True when part of the request is inside a range and part is not.
    """
    first, last = start or end, end or start          # a one-sided request is one day
    if first is None or last is None:                 # no dates: no boundary to cross
        return False
    ranges = (carryover_period(on), (_unbookable_from(on), date.max))
    for low, high in ranges:
        inside = _overlap(first, last, low, high)
        if inside is not None and inside != (first, last):   # partly in, partly out
            return True
    return False


def leave_day_split(
    start: date | None, end: date | None, days: float, on: date,
    holidays: list[dict] | None = None,
    season: tuple[date | None, date | None] = (None, None),
) -> LeaveSplit:
    """Split a request's days by what each pot can pay for. Pure.

    A request wholly on one side of a boundary uses its stored days, so holidays
    are only consulted when it crosses one. A count is capped at the stored days
    because those are what the person is charged, even if edited by hand.

    Args:
        start: The request's first day, or None.
        end: The request's last day, or None.
        days: The days the request costs.
        on: The rule date.
        holidays: The person's province's holiday rows, for a crossing request.
        season: The half-Friday season, for a crossing request.

    Returns:
        The split. With no dates, every day counts as payable by carry-over.
    """
    first, last = start or end, end or start          # a one-sided request is one day
    if first is None or last is None:                 # no dates: nothing to hold against the period
        return LeaveSplit(carryover_days=days, ahead_days=0.0, unbookable_days=0.0)

    def days_in(low: date, high: date) -> float:
        """The request's days falling inside ``low..high``."""
        inside = _overlap(first, last, low, high)
        if inside is None:                            # none of the request is in this range
            return 0.0
        if inside == (first, last):                   # all of it is: the stored days, no counting
            return days
        counted = calculate_business_days(inside[0], inside[1], holidays or [], season)
        return max(0.0, min(counted, days))           # never above what the request costs

    period_start, period_end = carryover_period(on)
    carryover_days = days_in(period_start, period_end)
    # The rest can still be unbookable, but the two never add up past the stored days.
    unbookable_days = min(days_in(_unbookable_from(on), date.max), days - carryover_days)
    # The whole period sits in next year or none of it does, so the days are all or nothing.
    ahead_days = carryover_days if period_start > date(on.year, 12, 31) else 0.0
    return LeaveSplit(carryover_days, ahead_days, unbookable_days)


def carryover_terms(carryover_days: float, on: date, today: date) -> tuple[float, float]:
    """What the cascade gets for carry-over, once expiry is taken into account.

    Normally carry-over pays for the days in its period. Once Mar 31 of that period
    has passed (created Mar 30 for Mar 31, approved Apr 2), the pot has been reset:
    those days are charged to nothing, rather than to the vacation that could not
    pay for them when the request was made.

    Args:
        carryover_days: The days dated inside the rule date's carry-over period.
        on: The rule date.
        today: Today in Toronto.

    Returns:
        ``(carryover_days, waived_days)`` for ``simulate_leave_impact``.
    """
    _, period_end = carryover_period(on)
    if period_end < today:                            # the period is over: nothing left to pay from
        return 0.0, carryover_days
    return carryover_days, 0.0


def _toronto_date(moment: datetime) -> date:
    """The Toronto calendar date of a stored moment.

    Args:
        moment: The moment; a naive value is UTC, as SQLite hands it back.

    Returns:
        The date in Toronto at that moment.
    """
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(TORONTO).date()


def rule_date_for(request) -> date:
    """The date whose rules decide which pot pays for this request.

    The date it was created, or the date of the last edit that changed its dates
    or days.

    Args:
        request: A ``RequestRecord``.

    Returns:
        ``rules_on`` when set, else the Toronto date of ``created_at``, else today
        for a row not saved yet.
    """
    if request.rules_on is not None:                  # an edit moved it
        return request.rules_on
    if request.created_at is not None:                # the normal case
        return _toronto_date(request.created_at)
    return toronto_today()                            # not saved yet, so it is being created today


def rule_date_from_fields(fields: dict, today: date) -> date:
    """``rule_date_for`` for a request in the SharePoint list-item shape.

    Args:
        fields: The request's fields; ``RulesOn`` and ``Created`` are read.
        today: Today in Toronto, used when neither is there or readable.

    Returns:
        The rule date.
    """
    rules_on = str(fields.get("RulesOn") or "")[:10]
    created = str(fields.get("Created") or "")
    try:
        if rules_on:                                  # an edit moved it
            return date.fromisoformat(rules_on)
        if created:                                   # SharePoint and Postgres both give the created time
            return _toronto_date(datetime.fromisoformat(created.replace("Z", "+00:00")))
    except ValueError:                                # an unreadable value should not stop a projection
        logger.warning("Unreadable rule date: RulesOn %r, Created %r", rules_on, created)
    return today


def bookable_until(sp_fields: dict, on: date) -> date:
    """The last date a salaried person can book vacation to, from one rule date.

    Computed, never stored. Vacation pays to Dec 31 of the rule date's year. From
    April on, next year's Jan 1 to Mar 31 is open too, but only to someone with
    Make-Up or carry-over to pay for it.

    Args:
        sp_fields: The person's Staff-Directory-shaped balances.
        on: The rule date.

    Returns:
        Mar 31 next year, or Dec 31 of the rule date's year.
    """
    year_end = date(on.year, 12, 31)
    _, period_end = carryover_period(on)
    if period_end <= year_end:                        # Jan to Mar: the period is already this year's
        return year_end
    make_up = float(sp_fields.get("CurrentOvertimeBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    return period_end if make_up > 0 or carryover > 0 else year_end


def booking_refusal(sp_fields: dict, leave_type: str, split: LeaveSplit, on: date) -> str | None:
    """The refusal when nothing can pay for a salaried Vacation/part-day request.

    This year's dates are never refused: vacation may go below zero, and the
    manager sees the red warning. Late requests are normal, so a date already past
    is never refused for being past. Hourly staff and every other leave type,
    sick days included, are never refused here.

    Args:
        sp_fields: The person's Staff-Directory-shaped balances (and SalaryHourly).
        leave_type: The request's leave type.
        split: How the request's days fall, from ``leave_day_split``.
        on: The rule date.

    Returns:
        The refusal sentence, or None when the request is allowed.
    """
    if is_hourly(sp_fields):                          # hourly: no vacation balance, never refused
        return None
    if leave_type not in LEAVE_BOOKING_TYPES:         # only vacation and part days are booked
        return None
    if split.unbookable_days > 0:                     # nothing can pay until the new year's balance arrives
        return f"Leave on these dates can be booked from January 1, {on.year + 1}."
    make_up = max(float(sp_fields.get("CurrentOvertimeBalance", 0) or 0), 0.0)   # a negative counts as 0
    carryover = max(float(sp_fields.get("CarryOver", 0) or 0), 0.0)
    have = make_up + carryover
    if split.ahead_days > have:                       # vacation cannot pay these days, and the rest falls short
        needed = f"{split.ahead_days:g} day{'' if split.ahead_days == 1 else 's'}"
        return (
            f"These dates need {needed} of carry-over or Make-Up and you have {have:g}. "
            "Vacation can only be used for this year's dates."
        )
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

async def _holidays_or_none(fields: dict) -> list[dict]:
    """The person's province's holidays, or none when they cannot be read.

    Args:
        fields: The person's Staff Directory fields (``Location`` picks the province).

    Returns:
        The holiday rows; [] with a logged warning on any failure, which counts
        plain weekdays instead.
    """
    try:
        return await get_holidays_for_province(map_location_to_province(fields.get("Location", "")))
    except Exception:  # noqa: BLE001 - never fail a request over this, weekdays are close enough
        logger.warning(
            "Holidays could not be read for %r, counting weekdays to split this request",
            fields.get("Location"), exc_info=True,
        )
        return []


async def leave_split_for(
    fields: dict, start: date | None, end: date | None, days: float, on: date,
) -> LeaveSplit:
    """``leave_day_split``, reading holidays only when the request crosses a boundary.

    Most requests do no extra read. If the province or holidays cannot be read,
    weekdays are counted instead: a projection a little off is better than a
    request that cannot be decided. Call it before taking the balance lock.

    Args:
        fields: The person's Staff Directory fields (``Location`` picks the province).
        start: The request's first day, or None.
        end: The request's last day, or None.
        days: The days the request costs.
        on: The rule date.

    Returns:
        The split.
    """
    if not leave_crosses_boundary(start, end, on):    # wholly on one side: no counting needed
        return leave_day_split(start, end, days, on)
    holidays = await _holidays_or_none(fields)
    return leave_day_split(start, end, days, on, holidays, get_half_friday_season(holidays))


async def booking_refusal_for(
    fields: dict, leave_type: str, start: date | None, end: date | None,
    partial_hours: float | None, on: date,
) -> str | None:
    """``booking_refusal`` for a form not saved yet, which has no stored days.

    This year's dates are never refused, so they read nothing. Next year's work out
    the days the way the submit path does, because the days are what is compared.

    Args:
        fields: The person's Staff Directory fields.
        leave_type: The form's leave type.
        start: The first day, or None.
        end: The last day, or None.
        partial_hours: A part day's hours, or None for full days.
        on: The rule date, today for a new request.

    Returns:
        The refusal sentence, or None when the request is allowed.
    """
    if is_hourly(fields) or leave_type not in LEAVE_BOOKING_TYPES:   # never refused
        return None
    first, last = start or end, end or start
    if first is None or last is None or last <= date(on.year, 12, 31):   # no dates, or this year's
        return None
    holidays = await _holidays_or_none(fields)
    season = get_half_friday_season(holidays)
    if partial_hours is not None:                     # a part day is its hours over 8
        days = float(partial_hours) / 8
    else:
        days = calculate_business_days(first, last, holidays, season)
    split = leave_day_split(first, last, days, on, holidays, season)
    return booking_refusal(fields, leave_type, split, on)


async def leave_carryover_terms(
    fields: dict, start: date | None, end: date | None, days: float, on: date, today: date | None = None,
) -> tuple[float, float]:
    """``(carryover_days, waived_days)`` for one leave request, ready for the cascade.

    Args:
        fields: The person's Staff Directory fields.
        start: The request's first day, or None.
        end: The request's last day, or None.
        days: The days the request costs.
        on: The rule date.
        today: Today in Toronto; defaults to now.

    Returns:
        The two values ``simulate_leave_impact`` and the cascade take.
    """
    split = await leave_split_for(fields, start, end, days, on)
    return carryover_terms(split.carryover_days, on, today or toronto_today())


async def cascade_current_year(
    employee_id: str | int, carryover_days: float | None = None, waived_days: float = 0.0,
) -> dict:
    """Run the Sick -> Make-Up -> Carry Over -> Vacation cascade on SharePoint.

    Reads the employee's pots, applies ``cascade_current_year_pure`` and writes the
    result back, looping until it settles. The second pass finds nothing overdrawn,
    so waived days are not used twice.

    Args:
        employee_id: Staff Directory item id.
        carryover_days: The most carry-over may pay; None means no cap.
        waived_days: The days charged to nothing because their carry-over expired.

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

        result = cascade_current_year_pure(sick, overtime, carryover, vacation, carryover_days, waived_days)

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
    sp_fields: dict, leave_type: str, days: float,
    carryover_days: float | None = None, waived_days: float = 0.0,
) -> dict | None:
    """Simulate balance impact of approving a leave request.

    Returns projected SP-style balance dict, or None for no-impact types. Spends
    Make-Up first (or Sick for a sick/personal day), then cascades the shortfall
    down Sick -> Make-Up -> Carry Over -> Vacation. Carry-over pays only for the
    days dated inside its period (``carryover_days``).

    Args:
        sp_fields: The person's Staff-Directory-shaped balances (and SalaryHourly).
        leave_type: The request's leave type.
        days: The days the leave costs.
        carryover_days: The most carry-over may pay; None means no cap.
        waived_days: The days charged to nothing because their carry-over expired.

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

    # One cascade for every date; carry-over is capped at the days it can pay for.
    result = cascade_current_year_pure(sick, overtime, carryover, vacation, carryover_days, waived_days)
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
