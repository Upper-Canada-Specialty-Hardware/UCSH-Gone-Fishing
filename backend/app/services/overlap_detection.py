"""Duplicate request detection — prevents overlapping date ranges for the same employee."""

import logging
from datetime import date, datetime

from app.config import settings
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)

BLOCKING_STATUSES = {"Pending", "Approved"}


class OverlapError(Exception):
    """Raised when a new request overlaps an existing one."""

    def __init__(self, request_type: str, conflicting_request: dict):
        self.request_type = request_type
        self.conflicting_request = conflicting_request
        super().__init__(f"Overlapping {request_type} request found")


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
