"""Admin lookup of the outbound email log.

Answers the support question "what did the system email this person, and did
SMTP2GO accept it" from the ``email_log`` table, so nobody has to search the
hosting logs. There is no dashboard page for it yet; opening the endpoint
URL in a browser is enough.
"""

import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query

from app.models import EmailLog
from app.models.mixins import utcnow
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
    """Shape one EmailLog row for the JSON response.

    Args:
        row: The stored attempt.

    Returns:
        A snake_case dict with the packed address strings expanded to lists.
    """
    return {
        "id": row.id,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "status": row.status,
        "to": unpack_addresses(row.to_addresses),
        "cc": unpack_addresses(row.cc_addresses),
        "subject": row.subject,
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
    limit: int = Query(100, ge=1, le=1000, description="Maximum rows to return"),
):
    """Every email attempt involving one person, newest first.

    Unauthenticated by design, like every other ``/admin/*`` endpoint: the
    admin link is distributed by management and must work with no token.

    Resolution order: an explicit ``address`` wins; otherwise the employee's
    address is read from the Staff Directory. If the directory has no such
    employee (wrong id, or SharePoint unreachable) the search still runs on
    the id alone and the response says so, because an outage is exactly when
    this lookup is needed.

    Args:
        employee_id: Staff Directory item id; matched against the primary id
            and, via the directory, against To/CC addresses.
        address: Email address to match in To/CC instead of the directory one.
        days: Window size in days, ending now. Defaults to 30, one full
            notification cycle; widen it for an older question.
        limit: Row cap.

    Returns:
        ``employee_id``, ``employee_name``, the ``address`` actually searched,
        ``directory_lookup`` ("ok" | "not_found" | "skipped"), the ``days``
        window, ``count``, and ``emails`` (serialised rows, newest first).

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
            directory_lookup = "not_found"                 # still search by id below

    rows = await find_emails(
        employee_id=employee_id,
        address=resolved,
        since=utcnow() - timedelta(days=days),
        limit=limit,
    )
    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "address": resolved or None,
        "directory_lookup": directory_lookup,
        "days": days,
        "count": len(rows),
        "emails": [_serialize(r) for r in rows],
    }
