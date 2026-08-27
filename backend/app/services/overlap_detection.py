"""Duplicate request detection — stops an employee holding the same dates twice.

Two rules define this module, and both changed deliberately:

1. Only an APPROVED request reserves dates. Balance is spent at approval, so a
   request still awaiting a manager has no claim on the dates yet. Treating
   Pending as blocking meant a request that stalled before reaching a manager
   held those dates permanently — and, because dashboards hide a pending
   request until it has both its day count and its manager, held them
   invisibly.

2. A conflict is raised when a manager attempts to APPROVE, not when the
   request is created. Blocking at creation ran ahead of the day calculation
   and the manager assignment, so a request rejected as a duplicate was left
   with no days, no manager and no location, and never reached anybody.
   Checking at approval also closes the opposite gap: two overlapping requests
   created before either is actioned can otherwise both be approved and both
   deduct balance.
"""

import logging
from datetime import date, datetime

from app.repositories import get_leave_request_repository, get_overtime_request_repository

logger = logging.getLogger(__name__)

# Only an approved absence reserves its dates — see rule 1 in the module docstring.
BLOCKING_STATUSES = {"Approved"}

# What one working day is worth, in the units the Days column uses. A request
# for less than this on a single date only partly occupies that date.
FULL_DAY = 1.0


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _as_days(value) -> float:
    """Read a Days value, treating anything unreadable as zero.

    Zero is the safe reading: it makes a request look whole-day rather than
    fractional, so an unparseable value blocks rather than quietly sharing a
    date.

    Args:
        value: Whatever the Days column held.

    Returns:
        The value as a float, or 0.0.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_fractional_day(start: date, end: date, days: float) -> bool:
    """Whether a request takes only part of a single date.

    Keyed on the day count rather than the leave type, because the count is
    what decides how much of the date is used — half a sick day and half a
    vacation day occupy a date the same way.

    Args:
        start: First date of the request.
        end: Last date of the request.
        days: Days the request costs.

    Returns:
        True for a single date costing more than nothing and less than a day.
    """
    return start == end and 0 < days < FULL_DAY


def _extract_lookup_id(fields: dict, field_prefix: str) -> int | None:
    """Extract the SP User lookup ID from a Person/Group field.

    Handles both form-created items (explicit LookupId int) and
    SP-created items (nested dict with LookupId key).
    """
    lid = fields.get(f"{field_prefix}LookupId")
    if lid is not None:
        try:
            return int(lid)
        except (ValueError, TypeError):
            pass
    nested = fields.get(field_prefix)
    if isinstance(nested, dict):
        try:
            return int(nested["LookupId"])
        except (KeyError, ValueError, TypeError):
            pass
    return None


async def check_leave_overlap(
    submitter_lookup_id: int,
    start_date: str,
    end_date: str,
    exclude_item_id: str | None = None,
    days: float = 0.0,
) -> dict | None:
    """Fetch the leave list, then match against it.

    Thin wrapper over `find_leave_conflict`. Callers that already hold the
    rows — the admin stuck-request view reads the whole list anyway — should
    use that directly rather than paying for a second fetch.

    Args:
        submitter_lookup_id: Microsoft 365 lookup id of the person taking leave.
        start_date: First date of the request being checked.
        end_date: Last date of that request.
        exclude_item_id: Request to skip, so one never matches itself.
        days: Days the request costs.

    Returns:
        Whatever `find_leave_conflict` returns.
    """
    # Nothing to compare without both dates, so skip the list read entirely.
    if not _parse_date(start_date) or not _parse_date(end_date):
        return None

    items = await get_leave_request_repository().get_all()
    return find_leave_conflict(
        items,
        submitter_lookup_id=submitter_lookup_id,
        start_date=start_date,
        end_date=end_date,
        exclude_item_id=exclude_item_id,
        days=days,
    )


def find_leave_conflict(
    items: list[dict],
    submitter_lookup_id: int,
    start_date: str,
    end_date: str,
    exclude_item_id: str | None = None,
    days: float = 0.0,
) -> dict | None:
    """Match a set of dates against leave rows already fetched. No I/O.

    Whole days clash on any shared date. Fractional days do not: half a day
    leaves the other half of that date free, so several can share one date
    until together they come to a full day. Without this, a second half day
    on a date was treated exactly like booking the whole date twice.

    The fractional total is accumulated across every approved part-day on the
    date rather than compared one at a time, so three half days cannot slip
    through by passing each pairwise comparison.

    Args:
        items: Leave rows in the {"id", "fields"} shape Graph returns.
        submitter_lookup_id: Microsoft 365 lookup id of the person taking leave.
        start_date: First date of the request being checked.
        end_date: Last date of that request.
        exclude_item_id: Request to skip, so one never matches itself.
        days: Days the request costs. Left at zero it reads as a whole day,
            which blocks — the conservative reading when Days has not been
            calculated yet.

    Returns:
        None when the dates are free, otherwise a dict describing the
        conflict. One caused by part-days adding up carries
        "day_already_booked".
    """
    new_start = _parse_date(start_date)
    new_end = _parse_date(end_date)
    if not new_start or not new_end:
        return None

    new_days = _as_days(days)
    new_is_fractional = _is_fractional_day(new_start, new_end, new_days)

    # Approved part-days sharing the candidate's single date, totalled below.
    shared_day: list[tuple[dict, date, float]] = []

    for item in items:
        if exclude_item_id and str(item.get("id")) == str(exclude_item_id):
            continue

        f = item.get("fields", {})

        if f.get("Status") not in BLOCKING_STATUSES:
            continue

        existing_lid = _extract_lookup_id(f, "SubmittedTest")
        if existing_lid != submitter_lookup_id:
            continue

        existing_start = _parse_date(f.get("StartDate"))
        existing_end = _parse_date(f.get("EndDate"))
        if not existing_start or not existing_end:
            continue

        # Overlap: start1 <= end2 AND start2 <= end1
        if not (new_start <= existing_end and existing_start <= new_end):
            continue

        existing_days = _as_days(f.get("Days"))
        if new_is_fractional and _is_fractional_day(existing_start, existing_end, existing_days):
            # Both are single dates and they overlap, so it is the same date.
            # Set aside to total up rather than treated as a clash on its own.
            shared_day.append((item, existing_start, existing_days))
            continue

        return {
            "item_id": item.get("id"),
            "start_date": str(existing_start),
            "end_date": str(existing_end),
            "status": f.get("Status"),
        }

    if shared_day:
        # Rounded before comparing so float noise cannot push an exact full
        # day over the line.
        booked = round(sum(d for _, _, d in shared_day), 3)
        if round(new_days + booked, 3) > FULL_DAY:
            item, existing_start, _ = shared_day[0]
            return {
                "item_id": item.get("id"),
                "start_date": str(existing_start),
                "end_date": str(existing_start),
                "status": item.get("fields", {}).get("Status"),
                "day_already_booked": booked,
            }

    return None


async def check_overtime_overlap(
    submitter_lookup_id: int,
    overtime_date: str,
    exclude_item_id: str | None = None,
) -> dict | None:
    """Fetch the overtime list, then match against it.

    Thin wrapper over `find_overtime_conflict`, mirroring the leave pair.

    Args:
        submitter_lookup_id: Microsoft 365 lookup id of the person.
        overtime_date: Date being checked.
        exclude_item_id: Request to skip, so one never matches itself.

    Returns:
        Whatever `find_overtime_conflict` returns.
    """
    if not _parse_date(overtime_date):
        return None  # nothing to compare; skip the list read

    items = await get_overtime_request_repository().get_all()
    return find_overtime_conflict(
        items,
        submitter_lookup_id=submitter_lookup_id,
        overtime_date=overtime_date,
        exclude_item_id=exclude_item_id,
    )


def find_overtime_conflict(
    items: list[dict],
    submitter_lookup_id: int,
    overtime_date: str,
    exclude_item_id: str | None = None,
) -> dict | None:
    """Match a date against overtime rows already fetched. No I/O.

    Overtime has no part-day rule: an entry either falls on the date or it
    does not. Hours are not capped the way a leave day is, so two entries on
    one date are treated as the same booking rather than added together.

    Args:
        items: Overtime rows in the {"id", "fields"} shape Graph returns.
        submitter_lookup_id: Microsoft 365 lookup id of the person.
        overtime_date: Date being checked.
        exclude_item_id: Request to skip, so one never matches itself.

    Returns:
        None when the date is free, otherwise a dict describing the clash.
    """
    new_date = _parse_date(overtime_date)
    if not new_date:
        return None

    for item in items:
        if exclude_item_id and str(item.get("id")) == str(exclude_item_id):
            continue

        f = item.get("fields", {})

        if f.get("Status") not in BLOCKING_STATUSES:
            continue

        existing_lid = _extract_lookup_id(f, "SubmittedBy")
        if existing_lid != submitter_lookup_id:
            continue

        existing_date = _parse_date(f.get("StartDate"))
        if not existing_date:
            continue

        if new_date == existing_date:
            return {
                "item_id": item.get("id"),
                "date": str(existing_date),
                "status": f.get("Status"),
            }

    return None


def find_overtime_conflict_for_row(items: list[dict], item: dict) -> dict | None:
    """Work out whether one pending overtime row is blocked by an approved entry.

    Args:
        items: Every overtime row, already fetched.
        item: The row being tested.

    Returns:
        The conflict dict, or None when the date is free or the submitter
        cannot be identified.
    """
    fields = item.get("fields", {})
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedBy")
    if not submitter_lookup_id:
        return None
    return find_overtime_conflict(
        items,
        submitter_lookup_id=submitter_lookup_id,
        overtime_date=fields.get("StartDate", ""),
        exclude_item_id=str(item.get("id")),
    )


def find_conflict_for_row(items: list[dict], item: dict) -> dict | None:
    """Work out whether one pending leave row is blocked by an approved absence.

    Convenience over `find_leave_conflict` for callers holding raw rows: it
    reads the submitter, dates and day count off the row itself, so nothing
    outside this module needs to know how a person column is shaped or how a
    missing day count should be read.

    Args:
        items: Every leave row, already fetched.
        item: The row being tested, in the {"id", "fields"} shape.

    Returns:
        The conflict dict, or None when the dates are free or the submitter
        cannot be identified.
    """
    fields = item.get("fields", {})
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedTest")
    if not submitter_lookup_id:
        return None  # nothing to compare against; not evidence of a clash
    return find_leave_conflict(
        items,
        submitter_lookup_id=submitter_lookup_id,
        start_date=fields.get("StartDate", ""),
        end_date=fields.get("EndDate", ""),
        exclude_item_id=str(item.get("id")),
        days=_as_days(fields.get("Days")),
    )


def find_requests_blocked_by(
    items: list[dict],
    approved_item_id: str | int,
    submitter_lookup_id: int,
    person_column: str,
    matcher,
) -> list[tuple[dict, dict]]:
    """Find the employee's pending rows that this approval has just stranded.

    Approving one absence can strand another the employee already submitted.
    Only approved requests reserve dates, so two overlapping requests sit
    quite happily pending until the first is approved - and the second then
    stops being approvable without anything marking it, or telling anyone.

    Only rows blocked by THIS approval are returned. A row already blocked by
    something else was reported when that happened, and repeating it every
    time anything is approved would train people to ignore the message.

    Args:
        items: Every row from the list, fetched after the approval landed so
            the approved row already reads as approved.
        approved_item_id: The request just approved.
        submitter_lookup_id: Whose requests to look at.
        person_column: The person column that list uses.
        matcher: `find_conflict_for_row` or `find_overtime_conflict_for_row`.

    Returns:
        (row, conflict) pairs, one per stranded request.
    """
    blocked = []
    for item in items:
        fields = item.get("fields", {})
        if fields.get("Status") != "Pending":
            continue
        if str(item.get("id")) == str(approved_item_id):
            continue
        if _extract_lookup_id(fields, person_column) != submitter_lookup_id:
            continue
        conflict = matcher(items, item)
        # Blocked by something else entirely, so not news caused by this.
        if conflict and str(conflict["item_id"]) == str(approved_item_id):
            blocked.append((item, conflict))
    return blocked


# --- Approval-time entry points -------------------------------------------------
#
# The shared approve handlers call these, so all three approval channels — the
# emailed link, the text reply and the dashboard button — are covered by one
# check rather than three copies of it.
#
# A conflict is reported in three pieces, because three different audiences act
# on it: the fact (what is already approved), the manager's action (reject, or
# cancel the other one) and the employee's (nothing to do; it will not be
# approved as it stands). Building the fact once keeps every channel and every
# email saying the same thing.


async def find_leave_conflict_for_request(request_id: str | int, fields: dict) -> dict | None:
    """Check a leave request against the employee's already-approved absences.

    Args:
        request_id: Id of the request being checked. Excluded from its own search.
        fields: SharePoint field values already fetched for that request.

    Returns:
        The conflict dict, or None when the dates are free or the submitter
        cannot be identified.
    """
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedTest")  # who the leave is for
    if not submitter_lookup_id:
        # No identifiable submitter means nothing to compare against. Let the
        # approval through rather than blocking on a lookup failure, but say so:
        # silence here would read as "checked, no conflict".
        logger.warning(
            "LR #%s — conflict check skipped: submitter could not be identified",
            request_id,
        )
        return None

    conflict = await check_leave_overlap(  # approved-only, and never matches itself
        submitter_lookup_id=submitter_lookup_id,
        start_date=fields.get("StartDate", ""),
        end_date=fields.get("EndDate", ""),
        exclude_item_id=str(request_id),
        days=_as_days(fields.get("Days")),  # decides whether it can share a date
    )
    if conflict:
        logger.info(
            "LR #%s — clashes with approved leave request #%s",
            request_id, conflict["item_id"],
        )
    return conflict


async def find_overtime_conflict_for_request(request_id: str | int, fields: dict) -> dict | None:
    """Check an overtime request against the employee's already-approved entries.

    Args:
        request_id: Id of the request being checked. Excluded from its own search.
        fields: SharePoint field values already fetched for that request.

    Returns:
        The conflict dict, or None when that date is free or the submitter
        cannot be identified.
    """
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedBy")  # overtime uses a different person column
    if not submitter_lookup_id:
        logger.warning(
            "OT #%s — conflict check skipped: submitter could not be identified",
            request_id,
        )
        return None

    conflict = await check_overtime_overlap(  # same date, approved only, self excluded
        submitter_lookup_id=submitter_lookup_id,
        overtime_date=fields.get("StartDate", ""),
        exclude_item_id=str(request_id),
    )
    if conflict:
        logger.info(
            "OT #%s — clashes with approved overtime request #%s",
            request_id, conflict["item_id"],
        )
    return conflict


def describe_leave_conflict(conflict: dict) -> str:
    """State what is already approved, without saying what to do about it.

    Kept short on purpose: this sentence travels over SMS with a wrapper around
    it, and a text past 160 characters is billed as two.

    Args:
        conflict: A conflict from `find_leave_conflict_for_request`.

    Returns:
        One sentence naming the approved request and the dates it holds.
    """
    if "day_already_booked" in conflict:
        # Part-days that fit alongside each other never produce a conflict at
        # all; this is only reached when they add up past a full day.
        return (
            f"{conflict['day_already_booked']} day of {conflict['start_date']} is "
            f"already approved (leave #{conflict['item_id']}), so this will not fit."
        )
    return (
        f"Leave request #{conflict['item_id']} is already approved for "
        f"{conflict['start_date']} to {conflict['end_date']}."
    )


def describe_overtime_conflict(conflict: dict) -> str:
    """State what is already approved on that date. Same length constraint.

    Args:
        conflict: A conflict from `find_overtime_conflict_for_request`.

    Returns:
        One sentence naming the approved entry.
    """
    return (
        f"Overtime request #{conflict['item_id']} for {conflict['date']} is "
        "already approved."
    )


_DESCRIBE = {"leave": describe_leave_conflict, "overtime": describe_overtime_conflict}


def _manager_action(conflict: dict) -> str:
    """What the manager can do about it. Neither option is taken automatically."""
    return f"Reject this request, or cancel #{conflict['item_id']} first."


def conflict_warning(conflict: dict | None, kind: str, audience: str) -> dict | None:
    """Build the warning block an approval or confirmation email renders.

    Args:
        conflict: The conflict, or None when there is nothing to warn about.
        kind: "leave" or "overtime", picking how the fact is worded.
        audience: "manager" or "employee" — same fact, different next step.

    Returns:
        {"heading", "detail", "action"} for the email partial, or None when
        there is no conflict, which renders nothing.
    """
    if not conflict:
        return None

    detail = _DESCRIBE[kind](conflict)
    if audience == "manager":
        return {
            "heading": "This request cannot be approved yet.",
            "detail": detail,
            "action": _manager_action(conflict),
        }
    return {
        "heading": "This overlaps time off you already have approved.",
        "detail": detail,
        # Deliberately not an instruction to do anything: the request has not
        # been cancelled and the decision belongs to the manager.
        "action": (
            "Your request has still gone to your manager, but it cannot be "
            f"approved while #{conflict['item_id']} stands. Speak to your manager "
            "if the approved request is the one that should change."
        ),
    }


async def find_leave_approval_conflict(request_id: str | int, fields: dict) -> str | None:
    """The refusal a manager sees when approving a clashing leave request.

    Args:
        request_id: Id of the request being approved.
        fields: SharePoint field values already fetched for that request.

    Returns:
        Fact and action as one sentence pair, or None when the dates are free.
    """
    conflict = await find_leave_conflict_for_request(request_id, fields)
    if not conflict:
        return None
    return f"{describe_leave_conflict(conflict)} {_manager_action(conflict)}"


async def find_overtime_approval_conflict(request_id: str | int, fields: dict) -> str | None:
    """The refusal a manager sees when approving a clashing overtime request.

    Args:
        request_id: Id of the request being approved.
        fields: SharePoint field values already fetched for that request.

    Returns:
        Fact and action as one sentence pair, or None when that date is free.
    """
    conflict = await find_overtime_conflict_for_request(request_id, fields)
    if not conflict:
        return None
    return f"{describe_overtime_conflict(conflict)} {_manager_action(conflict)}"
