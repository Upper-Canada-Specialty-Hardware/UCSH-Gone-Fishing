"""Admin lookup of the outbound email request/response log.

Answers the support questions "did the backend ask a service to email this
person, and what did it say back" (per person) and "what has gone out lately
across every service" (the recent view) from ``email_api_log``. Rows come from
both SMTP2GO and the UCSH mailer (Microsoft HVE); each row names its service.
The Email Log tab of the admin dashboard is the intended reader; the endpoint
URLs also work directly in a browser.
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
    find_duplicate_ids,
    find_exchanges,
    find_recent,
    log_coverage_start,
    normalize_address,
    service_for_url,
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


def _serialize(row: EmailApiLog, duplicate_ids: set[int] | None = None) -> dict:
    """Shape one log row for the JSON response.

    Args:
        row: The stored exchange, recipients loaded.
        duplicate_ids: Ids flagged as likely duplicates in the result set this
            row belongs to; the row's own id is looked up in it.

    Returns:
        A snake_case dict: the redacted request as an object, the response
        body verbatim, and the derived fields alongside. ``service`` names the
        email service that sent the row and ``message_id`` is that service's
        own id (a service-neutral alias of ``smtp2go_email_id``, which is kept
        for callers that still read it).
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
        "service": service_for_url(row.request_url),               # smtp2go | ucsh_mailer
        "message_id": row.smtp2go_email_id,                        # service-neutral name; mailer id lives here too
        "smtp2go_email_id": row.smtp2go_email_id,                  # kept for compatibility
        "smtp2go_request_id": row.smtp2go_request_id,
        "sender": row.sender,
        "subject": row.subject,
        "to": [r.address for r in row.recipients if r.field == "to"],
        "cc": [r.address for r in row.recipients if r.field == "cc"],
        "request_url": row.request_url,
        "likely_duplicate": row.id in (duplicate_ids or set()),    # same subject+recipient within 5 min
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
    """Every call (either service) that named one person, newest first.

    Behind the admin password like every other ``/admin/*`` endpoint (#228).

    Resolution: an explicit ``address`` wins; otherwise the employee's address
    is read from the Staff Directory. Only an address can be matched, because
    the log records what each service was asked to send and a service only knows
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
    duplicate_ids = find_duplicate_ids(rows)                       # flag repeat sends to this person
    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "address": resolved or None,
        "directory_lookup": directory_lookup,
        "days": days,
        "log_since": coverage.isoformat() if coverage else None,
        "count": len(rows),
        "emails": [_serialize(r, duplicate_ids) for r in rows],
    }


@router.get("/admin/email-log/recent")
async def admin_email_log_recent(
    hours: int = Query(
        24, ge=1, le=2160, description="How far back to look, in hours (default 24; max 90 days)"
    ),
    limit: int = Query(200, ge=1, le=1000, description="Maximum rows to return"),
    text: str | None = Query(
        None, description="Case-insensitive substring filter on recipient or subject"
    ),
):
    """Every recent email through any service, no person picked, newest first.

    This is the triage view: when an incident is "the system double-sent" or
    "did anything go out at all", an admin needs the whole stream, not one
    person's. The window is on ``attempted_at`` (indexed) and capped by
    ``limit``, so the scan stays bounded however busy the log is. Rows that
    repeat the same subject to the same recipient within five minutes are
    flagged (``likely_duplicate``) so a double-send stands out.

    Behind the admin password like every other ``/admin/*`` endpoint (#228).

    Args:
        hours: Window size in hours, ending now. Defaults to 24.
        limit: Row cap; ``truncated`` says when it was hit.
        text: Optional substring matched against recipient addresses and the
            subject, to narrow a noisy window.

    Returns:
        ``hours``, ``since`` (window start), ``log_since`` (when the log began),
        ``limit``, ``count``, ``truncated`` (whether the cap was reached, so
        more may exist earlier in the window), and ``emails`` newest first.
    """
    since = utcnow() - timedelta(hours=hours)
    # Ask for one more than the cap: if that extra row comes back the window
    # really does hold more than `limit`, so exactly `limit` rows is not
    # mistaken for a truncated result.
    fetched = await find_recent(since=since, limit=limit + 1, text=text)
    truncated = len(fetched) > limit
    rows = fetched[:limit]                                          # trim the probe row before serialising
    coverage = await log_coverage_start()
    duplicate_ids = find_duplicate_ids(rows)                       # mark bursts within the returned window
    return {
        "hours": hours,
        "since": since.isoformat(),
        "log_since": coverage.isoformat() if coverage else None,
        "limit": limit,
        "count": len(rows),
        "truncated": truncated,                                     # a row beyond the cap exists
        "emails": [_serialize(r, duplicate_ids) for r in rows],
    }


@router.post("/admin/email-log/test")
async def admin_email_log_test(body: TestEmailRequest):
    """Send a clearly labelled test email to one person and return its log row.

    This is how an admin checks the email path for a specific person without
    creating a request or involving a manager: the message goes through the
    same ``send_email`` call every system email uses, so it leaves the same
    ``email_api_log`` row, and that row is returned so the answer SMTP2GO gave
    is visible at once. Nothing in SharePoint is touched and no SMS is sent.

    Behind the admin password like every other ``/admin/*`` endpoint (#228).

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
