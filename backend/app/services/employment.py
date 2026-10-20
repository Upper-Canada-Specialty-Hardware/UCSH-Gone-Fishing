"""Employment types and the balance rules each one follows (#300).

The Staff Directory's SalaryHourly column holds one of three values, and they
follow two sets of rules:

- ``Salary``: every pot, with the Sick -> Make-Up -> Carry-over -> Vacation
  cascade. Vacation is the floor and may go below zero.
- ``Hourly`` and ``Hourly BC`` (every hourly employee, wherever they work):
  sick is the only balance and the floor. A sick day or a part day takes whole
  sick days from it and never overflows into another pot, so sick may go below
  zero. Vacation is not a balance at all: it is a counter of days taken this
  calendar year, worked out from approved requests. Overtime banks nothing.

The rules were first agreed for BC hourly staff and then applied to all hourly
staff (product owner, 2026-10-16), so "Hourly BC" is only a location detail now.
Every place that used to test ``SalaryHourly == "Hourly"`` asks this module
instead, so the rules live in one spot. Everything here is pure (no I/O).
"""

import math

SALARY = "Salary"            # salaried: the full cascade
HOURLY = "Hourly"            # hourly: sick only, no overflow, vacation counted
HOURLY_BC = "Hourly BC"      # older value for hourly staff in BC: same rules
HOURLY_TYPES = (HOURLY, HOURLY_BC)

VACATION = "Vacation"                       # leave kind counted for hourly staff
SICK_LEAVE = "Sick or Personal Day"         # spends sick for everyone
PARTIAL_DAY = "Half Day or Partial Day Off" # hourly: a whole sick day
# Working from home: a current leave type (#388) that costs no balance for anyone,
# because it is not time off. Named once so the approval, preview and routing code
# all test the same string.
WORK_FROM_HOME = "Work from Home"
# Leave that never touches a balance, on every approval path. Work from home joins
# bereavement and jury duty here: its approval moves nothing and runs no overflow step.
NO_COST_LEAVE = ("Bereavement", "Jury Duty", WORK_FROM_HOME)

# The column each type's floor lives in, and its wire pot name.
SICK_FIELD = "CurrentSickDayBalance"
VACATION_FIELD = "CurrentVacationBalance"


def employment_type(fields: dict | None) -> str:
    """The person's SalaryHourly value, trimmed.

    Args:
        fields: Staff Directory fields, or None when the person is unknown.

    Returns:
        "Salary", "Hourly", "Hourly BC", or "" when blank or unknown.
    """
    return str((fields or {}).get("SalaryHourly") or "").strip()  # None -> ""


def is_hourly(fields: dict | None) -> bool:
    """Whether the person is hourly (sick only, vacation counted).

    Args:
        fields: Staff Directory fields, or None.

    Returns:
        True for "Hourly" and "Hourly BC", wherever the person works.
    """
    return employment_type(fields) in HOURLY_TYPES


def leave_costs_balance(fields: dict | None, leave_type: str) -> bool:
    """Whether approving this kind of leave moves any balance for this person.

    Args:
        fields: The submitter's Staff Directory fields, or None (then the
            salaried rule applies, the same default as before).
        leave_type: The request's LeaveType.

    Returns:
        False for bereavement, jury duty and work from home; for hourly staff, False unless it
        is a sick or part day (their vacation is counted, not spent); True
        otherwise.
    """
    if leave_type in NO_COST_LEAVE:                       # nobody pays for these
        return False
    if is_hourly(fields):                                 # sick pot only
        return leave_type in (SICK_LEAVE, PARTIAL_DAY)
    return True                                           # salaried: every other kind


def overtime_costs_balance(fields: dict | None) -> bool:
    """Whether approving overtime moves a balance (adds to Make-Up).

    Args:
        fields: The submitter's Staff Directory fields, or None.

    Returns:
        False for hourly staff (paid, not banked); True for salaried.
    """
    return not is_hourly(fields)


def whole_sick_days(days: float) -> float:
    """The sick days an hourly request takes: always whole days, rounded up.

    A part day still uses a full sick day, and a stray fraction on a sick
    request rounds up too, so the sick pot only ever moves in whole days.

    Args:
        days: The request's Days value.

    Returns:
        The whole number of days as a float; 0.0 for zero or negative input.
    """
    if days <= 0:                                         # nothing requested
        return 0.0
    return float(math.ceil(round(days, 6)))               # round first: 1.0000001 stays 1


def floor_field(fields: dict | None) -> str:
    """The balance column that is this person's floor.

    Args:
        fields: Staff Directory fields, or None.

    Returns:
        The sick column for hourly; the vacation column for everyone else.
    """
    return SICK_FIELD if is_hourly(fields) else VACATION_FIELD


def _num(value) -> float:
    """A stored balance as a float; blank or unreadable counts as zero."""
    try:
        return float(value or 0)          # None/""/0 all collapse to 0.0
    except (TypeError, ValueError):
        return 0.0                        # a stray value never crashes a warning


def floor_warning(fields: dict | None, projected: dict | None) -> dict | None:
    """The warning to show when approving would take the floor below zero.

    A manager may still approve (it is their call and their responsibility), so
    this only flags it. Shown only when the floor ends below zero AND this
    request is what pushes it lower; a balance already negative that the
    request leaves alone is not flagged.

    Args:
        fields: The submitter's current Staff Directory fields.
        projected: SharePoint-style balances after approval (from a
            ``simulate_*`` function), or None when nothing would move.

    Returns:
        ``{"pot": "sick" | "vacation", "before": float, "after": float}``, or
        None when there is nothing to warn about.
    """
    if not fields or not projected:                       # unknown person or no change
        return None
    field = floor_field(fields)                           # sick for BC, vacation otherwise
    before = _num(fields.get(field))
    after = _num(projected.get(field, before))            # absent key: unchanged
    if after < 0 and after < before:                      # this request digs below zero
        return {
            "pot": "sick" if field == SICK_FIELD else "vacation",
            "before": round(before, 3),
            "after": round(after, 3),
        }
    return None


def floor_warning_text(warning: dict | None) -> str:
    """One plain sentence for a floor warning, for emails and texts.

    Args:
        warning: A ``floor_warning`` result, or None.

    Returns:
        The sentence, or "" when there is no warning.
    """
    if not warning:
        return ""
    label = "Sick" if warning["pot"] == "sick" else "Vacation"
    return (
        f"Approving this takes {label} below zero, to {warning['after']:g} days. "
        "It can still be approved; that is the manager's call."
    )


def vacation_days_counted(leave_rows: list[dict], year: int) -> float:
    """Vacation days one hourly person has taken in a calendar year.

    Counts approved Vacation requests whose start date is in ``year``. Nothing
    is stored: the counter is rebuilt from the requests each time, so it only
    ever goes up as requests are approved, falls back if one is refunded, and
    starts again at zero each January.

    Args:
        leave_rows: That one person's leave request fields (Status, LeaveType,
            Days, StartDate).
        year: The calendar year to count.

    Returns:
        The total days, never negative.
    """
    total = 0.0
    for row in leave_rows:
        if (row.get("Status") or "").strip() != "Approved":   # pending/refunded don't count
            continue
        if (row.get("LeaveType") or "") != VACATION:          # only vacation is counted
            continue
        # The employment type stamped on the request at decision time, when the
        # row carries it (Postgres #300). A vacation approved while the person was
        # salaried spent pots, so it is not an hourly "counted" day and must be
        # left out, matching the vacation_taken ledger counter. A row without the
        # stamp (SharePoint) keeps the old behaviour: this person is hourly now.
        stamped = row.get("RequestEmploymentType")
        if stamped is not None and stamped not in HOURLY_TYPES:
            continue
        start = str(row.get("StartDate") or "")[:4]           # "YYYY" of the first day
        if start != str(year):
            continue
        total += max(_num(row.get("Days")), 0.0)              # a bad negative never subtracts
    return round(total, 3)
