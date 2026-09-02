"""Admin lookup of what the system emailed one person.

Two sources, merged into one dated timeline:

- ``email_log``: the send record written for every SMTP2GO call since it was
  introduced on 2026-09-02, exact and complete from that day on.
- The records the backend was already keeping before then, read back and
  turned into dated sends (see ``services/email_history.py``): approval-state
  rows, processing claims, dashboard-link renewals, setup nudges, and the
  request items themselves. This is what covers the 30 days before the send
  log existed.

There is no dashboard page for it yet; opening the endpoint URL in a browser
is enough.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query

from app.models import EmailLog
from app.models.mixins import utcnow
from app.services.email_history import reconstruct_email_history
from app.services.email_log import (
    DEFAULT_WINDOW_DAYS,
    find_emails,
    normalize_address,
    unpack_addresses,
)
from app.services.employee import get_employee_by_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize(row: EmailLog) -> dict:
    """Shape one EmailLog row as a timeline entry.

    Args:
        row: The stored attempt.

    Returns:
        A snake_case dict in the same shape as a reconstructed event, plus the
        send-record fields (status, SMTP2GO ids, error) only a real record has.
    """
    return {
        "date": row.sent_at.isoformat() if row.sent_at else None,
        "date_precision": "exact",
        "subject": row.subject,
        "to": unpack_addresses(row.to_addresses),
        "also_to": ", ".join(unpack_addresses(row.cc_addresses)) or None,
        "source": "email_log",
        "request_type": None,
        "request_id": None,
        "note": None,
        "status": row.status,
        "primary_employee_id": row.primary_employee_id,
        "smtp2go_email_id": row.smtp2go_email_id,
        "smtp2go_request_id": row.smtp2go_request_id,
        "http_status": row.http_status,
        "error": row.error,
    }


@router.get("/admin/email-log")
async def admin_email_log(
    employee_id: str | None = Query(None, description="Staff Directory item id"),
    address: str | None = Query(
        None, description="Email address to match; overrides the directory lookup"
    ),
    days: int = Query(
        DEFAULT_WINDOW_DAYS, ge=1, le=3650, description="How far back to look (default 30)"
    ),
    limit: int = Query(200, ge=1, le=1000, description="Maximum entries to return"),
):
    """Every email the system sent one person in the window, newest first.

    Unauthenticated by design, like every other ``/admin/*`` endpoint: the
    admin link is distributed by management and must work with no token.

    Resolution order: an explicit ``address`` wins; otherwise the employee's
    name and address come from the Staff Directory. If the directory has no
    such employee the lookup still runs on whatever it has (the id alone finds
    carryover requests, renewals, and send-log rows) and the response says so.

    Args:
        employee_id: Staff Directory item id.
        address: Email address to use instead of the directory one.
        days: Window size in days, ending now. Defaults to 30.
        limit: Entry cap on the merged timeline.

    Returns:
        ``employee_id``, ``employee_name``, ``address``, ``directory_lookup``
        ("ok" | "not_found" | "skipped"), ``days``, ``count``, ``emails``
        (the merged timeline: each entry has ``date``, ``date_precision``,
        ``subject``, ``to``, ``also_to``, ``source``; send-log entries add
        ``status`` and the SMTP2GO ids), ``notes`` (what is not recorded or
        could not be read), and ``latest_dashboard_link_email_at``.

    Raises:
        HTTPException: 400 when neither ``employee_id`` nor ``address`` is given.
    """
    if not employee_id and not address:
        raise HTTPException(status_code=400, detail="Provide employee_id or address")

    employee_name = None
    directory_lookup = "skipped"                           # no id given, nothing to look up
    resolved = normalize_address(address)                  # explicit address wins
    if employee_id:
        employee = await get_employee_by_id(employee_id)   # Graph read; None on any failure
        if employee:
            directory_lookup = "ok"
            employee_name = employee["fields"].get("Title")
            if not resolved:
                resolved = normalize_address(employee["fields"].get("EmailAddress"))
        else:
            directory_lookup = "not_found"                 # still search on what we have

    since = utcnow() - timedelta(days=days)

    # What the backend can prove it sent before the send log existed.
    history = await reconstruct_email_history(
        employee_id=employee_id,
        employee_name=employee_name,
        address=resolved or None,
        since=since,
    )
    # The send log itself, exact from 2026-09-02 on.
    rows = await find_emails(
        employee_id=employee_id, address=resolved, since=since, limit=limit,
    )

    timeline = history["events"] + [_serialize(r) for r in rows]
    timeline.sort(key=lambda e: e["date"] or "", reverse=True)   # ISO strings sort by time

    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "address": resolved or None,
        "directory_lookup": directory_lookup,
        "days": days,
        "count": len(timeline),
        "emails": timeline[:limit],
        "notes": history["notes"],
        "latest_dashboard_link_email_at": history["latest_dashboard_link_email_at"],
    }
