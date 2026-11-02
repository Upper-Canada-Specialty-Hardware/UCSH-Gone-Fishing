"""Admin lookup of the outbound/inbound SMS log: the Text Log (#494).

The SMS counterpart of ``routes/email_api_log.py``. It answers "what has the
system texted this person (or about this request), what did Twilio say, did it
get delivered, and what did this person text back", from ``sms_api_log`` and
``sms_reply_log``. Developers have no Twilio console, so this is the only
evidence. The Text Log tab of the admin dashboard is the intended reader; the
endpoint URLs also work directly in a browser. Behind the admin password like
every other ``/admin/*`` endpoint (#228); registered in ``main.py`` the same way.
"""

import json
import logging
from datetime import timedelta

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.models import SmsApiLog, SmsReplyLog
from app.models.mixins import utcnow
from app.services.employee import get_employee_by_id
from app.services.sms import send_sms
from app.services.sms_api_log import (
    DEFAULT_WINDOW_DAYS,
    KIND_TEST,
    as_utc,
    find_recent_replies,
    find_recent_sends,
    find_replies,
    find_sends,
    last10,
    log_coverage_start,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# What the directory lookup found for the requested employee id.
LOOKUP_OK = "ok"                # employee found, cell number read
LOOKUP_NOT_FOUND = "not_found"  # no such id, or SharePoint unreachable
LOOKUP_NO_NUMBER = "no_number"  # employee found, CellNumber blank
LOOKUP_SKIPPED = "skipped"      # no id given, nothing to look up

# The valid request domains for the "by request" lookup, so a typo gives a clean
# 400 rather than an empty list that reads as "nothing sent".
REQUEST_KINDS = {"leave", "overtime", "carryover-payout"}

# The admin test text. SMS bodies carry no links, and the wording tells the
# recipient no reply is needed so a test never looks like a real approval ask.
TEST_SMS_BODY = (
    "This is a test text from the UCSH Out of Office admin dashboard, sent to "
    "confirm that texts from the system reach this phone. No reply needed."
)


class TestSmsRequest(BaseModel):
    """Who to text the test to: an employee id, or a number directly."""

    employee_id: str | None = None
    number: str | None = None


async def _resolve_number(
    employee_id: str | None, number: str | None
) -> tuple[str | None, str | None, str]:
    """Turn an employee id or an explicit number into the number to use.

    Args:
        employee_id: Staff Directory item id, looked up when given.
        number: Explicit number; wins over the directory value.

    Returns:
        ``(employee_name, number, directory_lookup)``. ``number`` is the value
        to send to / match on (None when nothing usable was found), and
        ``directory_lookup`` is one of the LOOKUP_* values saying what the
        directory said.
    """
    employee_name = None
    directory_lookup = LOOKUP_SKIPPED
    resolved = (number or "").strip() or None                      # explicit number wins
    if employee_id:
        employee = await get_employee_by_id(employee_id)           # staff store read; None on failure
        if employee:
            employee_name = employee["fields"].get("Title")
            if not resolved:
                cell = (employee["fields"].get("CellNumber") or "").strip()
                resolved = cell or None
            directory_lookup = LOOKUP_OK if resolved else LOOKUP_NO_NUMBER
        else:
            directory_lookup = LOOKUP_NOT_FOUND
    return employee_name, resolved, directory_lookup


def _serialize_send(row: SmsApiLog) -> dict:
    """Shape one send row for the JSON response (snake_case).

    Args:
        row: The stored send attempt.

    Returns:
        A dict with ``type`` ``"send"``, the timing, the recipient and body, the
        Twilio answer, and the delivery fields (``delivery_history`` parsed back
        into a list).
    """
    history = []
    if row.delivery_history:
        try:
            history = json.loads(row.delivery_history)
        except ValueError:
            history = []
    return {
        "type": "send",
        "id": row.id,
        "at": as_utc(row.attempted_at).isoformat() if row.attempted_at else None,
        "duration_ms": row.duration_ms,
        "kind": row.kind,
        "request_kind": row.request_kind,
        "request_item_id": row.request_item_id,
        "outbox_id": row.outbox_id,
        "request_id": row.request_id,
        "inbound_reply_id": row.inbound_reply_id,
        "to_number": row.to_number,
        "from_number": row.from_number,
        "body": row.body,
        "http_status": row.http_status,
        "twilio_sid": row.twilio_sid,
        "twilio_status": row.twilio_status,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "no_response_reason": row.no_response_reason,
        "response_body": row.response_body,
        "delivery_status": row.delivery_status,
        "delivery_error_code": row.delivery_error_code,
        "delivery_updated_at": as_utc(row.delivery_updated_at).isoformat() if row.delivery_updated_at else None,
        "delivery_history": history if isinstance(history, list) else [],
    }


def _serialize_reply(row: SmsReplyLog) -> dict:
    """Shape one inbound reply row for the JSON response (snake_case).

    Args:
        row: The stored inbound text and the handler's verdict.

    Returns:
        A dict with ``type`` ``"reply"``, what we received, what it parsed to,
        and the handler's outcome.
    """
    return {
        "type": "reply",
        "id": row.id,
        "at": as_utc(row.received_at).isoformat() if row.received_at else None,
        "message_sid": row.message_sid,
        "from_number": row.from_number,
        "to_number": row.to_number,
        "body": row.body,
        "signature_ok": row.signature_ok,
        "decision": row.decision,
        "request_type": row.request_type,
        "item_id": row.item_id,
        "reason": row.reason,
        "sender_staff_id": row.sender_staff_id,
        "outcome": row.outcome,
        "error_text": row.error_text,
    }


@router.get("/admin/sms-log")
async def admin_sms_log(
    employee_id: str | None = Query(None, description="Staff Directory item id"),
    number: str | None = Query(None, description="Phone number to match; overrides the directory lookup"),
    request_kind: str | None = Query(None, description="leave | overtime | carryover-payout"),
    item_id: str | None = Query(None, description="Request item id (with request_kind)"),
    days: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=3650, description="How far back to look (default 30)"),
    limit: int = Query(200, ge=1, le=1000, description="Maximum rows of each kind"),
):
    """Every text to/from one person, or about one request, newest first.

    Two scopes. By person: an explicit ``number`` wins, otherwise the employee's
    cell is read from the Staff Directory; sends are matched on the recipient's
    last ten digits and replies on the sender's, so formatting never defeats the
    match. By request: ``request_kind`` + ``item_id`` match the request a text
    was about. The result carries both the ``sends`` and the inbound ``replies``.

    Args:
        employee_id: Staff Directory item id, resolved to a cell number.
        number: Explicit number to match instead of the directory one.
        request_kind: Request domain to match (with ``item_id``).
        item_id: Request item id to match (with ``request_kind``).
        days: Window size in days, ending now. Defaults to 30.
        limit: Row cap per kind.

    Returns:
        The scope echoed back (``employee_id``, ``employee_name``, ``number``,
        ``directory_lookup``, ``request_kind``, ``item_id``), the ``days``
        window, ``log_since`` (when the log began), and ``sends`` / ``replies``
        newest first.

    Raises:
        HTTPException: 400 when no usable scope was given, or ``request_kind`` is
            not one of the three domains.
    """
    by_request = bool(request_kind or item_id)
    if by_request and (not request_kind or not item_id):
        raise HTTPException(status_code=400, detail="Provide both request_kind and item_id")
    if request_kind and request_kind not in REQUEST_KINDS:
        raise HTTPException(status_code=400, detail=f"request_kind must be one of {sorted(REQUEST_KINDS)}")
    if not by_request and not employee_id and not number:
        raise HTTPException(status_code=400, detail="Provide employee_id, number, or request_kind+item_id")

    employee_name = None
    resolved_number = None
    directory_lookup = LOOKUP_SKIPPED
    digits = None
    if not by_request:
        employee_name, resolved_number, directory_lookup = await _resolve_number(employee_id, number)
        digits = last10(resolved_number) if resolved_number else None

    since = utcnow() - timedelta(days=days)
    sends = await find_sends(
        digits=digits, request_kind=request_kind, item_id=item_id, since=since, limit=limit,
    )
    replies = await find_replies(
        digits=digits, request_kind=request_kind, item_id=item_id, since=since, limit=limit,
    )
    coverage = await log_coverage_start()
    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "number": resolved_number,
        "directory_lookup": directory_lookup,
        "request_kind": request_kind,
        "item_id": item_id,
        "days": days,
        "log_since": coverage.isoformat() if coverage else None,
        "count": len(sends) + len(replies),
        "sends": [_serialize_send(r) for r in sends],
        "replies": [_serialize_reply(r) for r in replies],
    }


@router.get("/admin/sms-log/recent")
async def admin_sms_log_recent(
    hours: int = Query(24, ge=1, le=2160, description="How far back to look, in hours (default 24; max 90 days)"),
    limit: int = Query(200, ge=1, le=1000, description="Maximum rows of each kind"),
    text: str | None = Query(None, description="Case-insensitive substring filter on body or number"),
):
    """Every recent text, sent and received, no person picked, newest first.

    The triage view: "did anything go out at all" and "what came back". Both
    windows are on an indexed timestamp and capped by ``limit``, so the scan
    stays bounded.

    Args:
        hours: Window size in hours, ending now. Defaults to 24.
        limit: Row cap per kind.
        text: Optional substring matched against the body or the number.

    Returns:
        ``hours``, ``since``, ``log_since``, ``limit``, ``count``, and ``sends`` /
        ``replies`` newest first.
    """
    since = utcnow() - timedelta(hours=hours)
    sends = await find_recent_sends(since=since, limit=limit, text=text)
    replies = await find_recent_replies(since=since, limit=limit, text=text)
    coverage = await log_coverage_start()
    return {
        "hours": hours,
        "since": since.isoformat(),
        "log_since": coverage.isoformat() if coverage else None,
        "limit": limit,
        "count": len(sends) + len(replies),
        "sends": [_serialize_send(r) for r in sends],
        "replies": [_serialize_reply(r) for r in replies],
    }


@router.post("/admin/sms-log/test")
async def admin_sms_log_test(body: TestSmsRequest):
    """Text a clearly labelled test to one person and return its log row.

    How an admin checks the SMS path for a specific person without creating a
    request or involving a manager: the text goes through the same ``send_sms``
    every system text uses, so it leaves the same ``sms_api_log`` row, and that
    row is returned so Twilio's answer is visible at once. Nothing in SharePoint
    is touched and no email is sent.

    Args:
        body: ``employee_id`` (resolved through the Staff Directory) or an
            explicit ``number``.

    Returns:
        ``employee_id``, ``employee_name``, ``number``, ``directory_lookup`` and
        ``send``: the serialised log row for this send, whatever Twilio answered.

    Raises:
        HTTPException: 400 when neither field is given, or no number could be
            resolved (the detail says which directory case it was).
    """
    if not body.employee_id and not body.number:
        raise HTTPException(status_code=400, detail="Provide employee_id or number")
    employee_name, resolved_number, directory_lookup = await _resolve_number(
        body.employee_id, body.number
    )
    if not resolved_number:
        raise HTTPException(
            status_code=400,
            detail={
                LOOKUP_NOT_FOUND: "The Staff Directory has no employee with that id",
                LOOKUP_NO_NUMBER: "That employee has no cell number in the Staff Directory",
            }.get(directory_lookup, "No number to text"),
        )

    started = utcnow()                                             # to find this send's row below
    try:
        await send_sms(resolved_number, TEST_SMS_BODY, kind=KIND_TEST)
    except httpx.HTTPError as e:
        # The row is already written; the caller wants Twilio's answer, not a 502.
        logger.warning("Test text did not go through: %s", e)
    except Exception as e:                                         # a bad number raises too; row still written
        logger.warning("Test text could not be sent: %s", e)

    rows = await find_sends(digits=last10(resolved_number), since=started - timedelta(seconds=5), limit=1)
    return {
        "employee_id": body.employee_id,
        "employee_name": employee_name,
        "number": resolved_number,
        "directory_lookup": directory_lookup,
        "send": _serialize_send(rows[0]) if rows else None,
    }
