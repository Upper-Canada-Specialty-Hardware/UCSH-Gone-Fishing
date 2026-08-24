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

from app.config import settings
from app.graph.sharepoint import sp_client

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

    items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
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
    """Check for overlapping overtime requests for the same employee (same date).

    Returns None if no overlap, or a dict describing the first conflict.
    """
    new_date = _parse_date(overtime_date)
    if not new_date:
        return None

    items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)

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


# --- Approval-time entry points -------------------------------------------------
#
# The shared approve handlers call these, so all three approval channels — the
# emailed link, the text reply and the dashboard button — are covered by one
# check rather than three copies of it.


async def find_leave_approval_conflict(request_id: str | int, fields: dict) -> str | None:
    """Check a leave request against the employee's already-approved absences.

    Args:
        request_id: Id of the request being approved. Excluded from its own search.
        fields: SharePoint field values already fetched for that request.

    Returns:
        A sentence naming the conflicting request, written for the manager who
        is trying to approve, or None when the dates are free.
    """
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedTest")  # who the leave is for
    if not submitter_lookup_id:
        # No identifiable submitter means nothing to compare against. Let the
        # approval through rather than blocking on a lookup failure, but say so:
        # silence here would read as "checked, no conflict".
        logger.warning(
            "LR #%s — approval conflict check skipped: submitter could not be identified",
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
    if not conflict:
        return None

    logger.info(
        "LR #%s — approval blocked: overlaps approved leave request #%s",
        request_id, conflict["item_id"],
    )
    # Kept short on purpose: the SMS channel sends this same sentence, and a
    # text over 160 characters is billed as two.
    if "day_already_booked" in conflict:
        # Part-days that fit alongside each other never reach here; this is
        # the case where they add up past a full day.
        return (
            f"{conflict['day_already_booked']} day is already approved for "
            f"{conflict['start_date']} (leave #{conflict['item_id']}), so this "
            f"would exceed one day. Reject it, or cancel #{conflict['item_id']} first."
        )
    return (
        f"This overlaps leave request #{conflict['item_id']}, approved for "
        f"{conflict['start_date']} to {conflict['end_date']}. Reject this request, "
        f"or cancel #{conflict['item_id']} first."
    )


async def find_overtime_approval_conflict(request_id: str | int, fields: dict) -> str | None:
    """Check an overtime request against the employee's already-approved entries.

    Args:
        request_id: Id of the request being approved. Excluded from its own search.
        fields: SharePoint field values already fetched for that request.

    Returns:
        A sentence naming the conflicting entry, written for the manager who is
        trying to approve, or None when that date is free.
    """
    submitter_lookup_id = _extract_lookup_id(fields, "SubmittedBy")  # overtime uses a different person column
    if not submitter_lookup_id:
        logger.warning(
            "OT #%s — approval conflict check skipped: submitter could not be identified",
            request_id,
        )
        return None

    conflict = await check_overtime_overlap(  # same date, approved only, self excluded
        submitter_lookup_id=submitter_lookup_id,
        overtime_date=fields.get("StartDate", ""),
        exclude_item_id=str(request_id),
    )
    if not conflict:
        return None

    logger.info(
        "OT #%s — approval blocked: conflicts with approved overtime request #%s",
        request_id, conflict["item_id"],
    )
    # Same length constraint as the leave message above.
    return (
        f"Overtime request #{conflict['item_id']} for {conflict['date']} is already "
        f"approved. Reject this request, or cancel #{conflict['item_id']} first."
    )
