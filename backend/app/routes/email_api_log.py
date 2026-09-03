"""Admin lookup of the SMTP2GO request/response log.

Answers the support question "did the backend ask SMTP2GO to email this
person, and what did SMTP2GO say back" from ``email_api_log``. The Email Log
tab of the admin dashboard is the intended reader; the endpoint URL also
works directly in a browser.
"""

import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.graph.email import send_email
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

# The test email an admin can send from the Email Log tab.
TEST_SUBJECT = "Leave system email test"
TORONTO = ZoneInfo("America/Toronto")


class TestEmailRequest(BaseModel):
    """Who to send the test email to: an employee id, or an address directly."""

    employee_id: str | None = None
    address: str | None = None


async def _resolve_address(
    employee_id: str | None, address: str | None
) -> tuple[str | None, str, str]:
    """Turn an employee id or an explicit address into the address to use.

    Args:
        employee_id: Staff Directory item id, looked up when given.
        address: Explicit address; wins over the directory value.

    Returns:
        ``(employee_name, address, directory_lookup)``. The address is
        normalised and "" when nothing usable was found; ``directory_lookup``
        is one of the LOOKUP_* values saying what the directory said.
    """
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
    return employee_name, resolved, directory_lookup


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

    employee_name, resolved, directory_lookup = await _resolve_address(employee_id, address)

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


@router.post("/admin/email-log/test")
async def admin_email_log_test(body: TestEmailRequest):
    """Send a clearly labelled test email to one person and return its log row.

    This is how an admin checks the email path for a specific person without
    creating a request or involving a manager: the message goes through the
    same ``send_email`` call every system email uses, so it leaves the same
    ``email_api_log`` row, and that row is returned so the answer SMTP2GO gave
    is visible at once. Nothing in SharePoint is touched and no SMS is sent.

    Unauthenticated like every other ``/admin/*`` endpoint.

    Args:
        body: ``employee_id`` (resolved through the Staff Directory) or an
            explicit ``address``.

    Returns:
        ``employee_id``, ``employee_name``, ``address``, ``directory_lookup``
        and ``email``: the serialised log row for this send, whatever SMTP2GO
        answered. A send that never reached SMTP2GO still has a row.

    Raises:
        HTTPException: 400 when neither field is given, or when no address
            could be resolved (the detail says which directory case it was).
    """
    if not body.employee_id and not body.address:
        raise HTTPException(status_code=400, detail="Provide employee_id or address")
    employee_name, resolved, directory_lookup = await _resolve_address(
        body.employee_id, body.address
    )
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail={
                LOOKUP_NOT_FOUND: "The Staff Directory has no employee with that id",
                LOOKUP_NO_ADDRESS: "That employee has no email address in the Staff Directory",
            }.get(directory_lookup, "No address to send to"),
        )

    started = utcnow()                                             # to find this send's row below
    stamp = started.astimezone(TORONTO).strftime("%b %d, %Y %I:%M %p")
    html = (
        "<p>This is a test email from the leave system's admin dashboard, sent at "
        f"{stamp} Toronto time to confirm that email from the system reaches this "
        "mailbox.</p><p>No action is needed.</p>"
    )
    try:
        await send_email(to=[resolved], subject=TEST_SUBJECT, html_body=html)
    except httpx.HTTPError as e:
        # The row is already written; the caller wants SMTP2GO's answer, not a 502.
        logger.warning("Test email to %s did not go through: %s", resolved, e)

    rows = await find_exchanges(
        address=resolved, since=started - timedelta(seconds=5), limit=1
    )
    return {
        "employee_id": body.employee_id,
        "employee_name": employee_name,
        "address": resolved,
        "directory_lookup": directory_lookup,
        "email": _serialize(rows[0]) if rows else None,
    }
