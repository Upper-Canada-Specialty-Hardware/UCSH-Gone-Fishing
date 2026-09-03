"""Admin lookup of the SMTP2GO request/response log.

Answers the support question "did the backend ask SMTP2GO to email this
person, and what did SMTP2GO say back" from ``email_api_log``. The Email Log
tab of the admin dashboard is the intended reader; the endpoint URL also
works directly in a browser.
"""

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query

from app.models import EmailApiLog
from app.models.mixins import utcnow
from app.services.email_api_log import (
    DEFAULT_WINDOW_DAYS,
    as_utc,
    find_exchanges,
    log_coverage_start,
    normalize_address,
)
from app.services.employee import get_employee_by_id

logger = logging.getLogger(__name__)
router = APIRouter()

# What the directory lookup found for the requested employee id.
LOOKUP_OK = "ok"                  # employee found, address read
LOOKUP_NOT_FOUND = "not_found"    # no such id, or SharePoint unreachable
LOOKUP_NO_ADDRESS = "no_address"  # employee found, EmailAddress blank
LOOKUP_SKIPPED = "skipped"        # no id given, nothing to look up


def _serialize(row: EmailApiLog) -> dict:
    """Shape one log row for the JSON response.

    Args:
        row: The stored exchange, recipients loaded.

    Returns:
        A snake_case dict: the redacted request as an object, the response
        body verbatim, and the derived fields alongside.
    """
    try:
        request = json.loads(row.request_json)                     # stored as JSON text
    except ValueError:
        request = row.request_json                                 # never expected; show raw
    return {
        "id": row.id,
        "attempted_at": as_utc(row.attempted_at).isoformat() if row.attempted_at else None,
        "duration_ms": row.duration_ms,
        "outcome": row.outcome,
        "http_status": row.http_status,
        "succeeded": row.succeeded_count,
        "failed": row.failed_count,
        "smtp2go_email_id": row.smtp2go_email_id,
        "smtp2go_request_id": row.smtp2go_request_id,
        "sender": row.sender,
        "subject": row.subject,
        "to": [r.address for r in row.recipients if r.field == "to"],
        "cc": [r.address for r in row.recipients if r.field == "cc"],
        "request_url": row.request_url,
        "request": request,
        "response_body": row.response_body,
        "no_response_reason": row.no_response_reason,
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
    """Every SMTP2GO call that named one person, newest first.

    Unauthenticated by design, like every other ``/admin/*`` endpoint: the
    admin link is distributed by management and must work with no token.

    Resolution: an explicit ``address`` wins; otherwise the employee's address
    is read from the Staff Directory. Only an address can be matched, because
    the log records what SMTP2GO was asked to send and SMTP2GO only knows
    addresses. So an employee with no directory address, or an id the
    directory cannot find, gets an empty list and a ``directory_lookup``
    value saying why. That empty answer is itself a finding: no address
    means the code could never have emailed them.

    Args:
        employee_id: Staff Directory item id, resolved to an address.
        address: Email address to match in To/CC instead of the directory one.
        days: Window size in days, ending now. Defaults to 30.
        limit: Row cap.

    Returns:
        ``employee_id``, ``employee_name``, the ``address`` searched,
        ``directory_lookup`` (ok | not_found | no_address | skipped), the
        ``days`` window, ``log_since`` (when the log began, so a short table
        is not misread as "nothing sent"), ``count`` and ``emails``.

    Raises:
        HTTPException: 400 when neither ``employee_id`` nor ``address`` is given.
    """
    if not employee_id and not address:
        raise HTTPException(status_code=400, detail="Provide employee_id or address")

    employee_name = None
    directory_lookup = LOOKUP_SKIPPED
    resolved = normalize_address(address)                          # explicit address wins
    if employee_id:
        employee = await get_employee_by_id(employee_id)           # Graph read; None on any failure
        if employee:
            employee_name = employee["fields"].get("Title")
            if not resolved:
                resolved = normalize_address(employee["fields"].get("EmailAddress"))
            directory_lookup = LOOKUP_OK if resolved else LOOKUP_NO_ADDRESS
        else:
            directory_lookup = LOOKUP_NOT_FOUND

    rows = await find_exchanges(
        address=resolved,                                          # "" -> no query, empty list
        since=utcnow() - timedelta(days=days),
        limit=limit,
    )
    coverage = await log_coverage_start()
    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "address": resolved or None,
        "directory_lookup": directory_lookup,
        "days": days,
        "log_since": coverage.isoformat() if coverage else None,
        "count": len(rows),
        "emails": [_serialize(r) for r in rows],
    }
