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


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


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
) -> dict | None:
    """Check for overlapping leave requests for the same employee.

    Returns None if no overlap, or a dict describing the first conflict.
    """
    new_start = _parse_date(start_date)
    new_end = _parse_date(end_date)
    if not new_start or not new_end:
        return None

    items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)

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
        if new_start <= existing_end and existing_start <= new_end:
            return {
                "item_id": item.get("id"),
                "start_date": str(existing_start),
                "end_date": str(existing_end),
                "status": f.get("Status"),
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
    )
    if not conflict:
        return None

    logger.info(
        "LR #%s — approval blocked: overlaps approved leave request #%s",
        request_id, conflict["item_id"],
    )
    return (
        f"This overlaps leave request #{conflict['item_id']}, already approved for "
        f"{conflict['start_date']} to {conflict['end_date']}. Reject this request, "
        "or cancel the approved one first."
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
    return (
        f"Overtime request #{conflict['item_id']} for {conflict['date']} is already "
        "approved. Reject this request, or cancel the approved one first."
    )
