"""Record and query the SMS send/reply log, the Text Log's backing store.

The SMS counterpart of ``services/email_api_log.py``. ``send_sms`` calls
``record_send`` once per attempt, the inbound webhook calls ``record_reply`` and
``update_reply`` once per inbound text, and the status webhook calls
``apply_delivery_update`` for each delivery callback. The admin Text Log reads it
all back through ``find_sends`` / ``find_replies`` (by number or by request) and
``find_recent_*`` (the whole stream). Everything here is bookkeeping around a
send that has already happened or an inbound that has already been answered: a
failed write is logged and swallowed, and never changes what the caller does.

Kept deliberately apart, like the email log:

* ``classify_twilio_response`` is pure. It reads a Twilio answer into a small
  struct and never touches the database.
* ``record_*`` / ``apply_delivery_update`` persist, and never raise.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, func, or_, select

from app.database import async_session
from app.models import SmsApiLog, SmsReplyLog

logger = logging.getLogger(__name__)

# Default lookup window: the admin question is "the past 30 days", matching the
# email log and the dashboard link lifetime.
DEFAULT_WINDOW_DAYS = 30

# A Twilio answer is a few hundred bytes. The cap only stops an unexpected HTML
# error page from bloating a row.
RESPONSE_MAX_CHARS = 8000

# The kind of text a row records. Outbox sends pass their message_type through,
# so these are the common values plus the three this module sets itself.
KIND_REPLY = "reply"      # an instant answer to an inbound text
KIND_TEST = "test"        # an admin test send
KIND_OTHER = "other"      # a legacy caller that passed no kind

# Which branch the inbound webhook took, stored on the reply row. One per early
# return in routes/twilio.py receive_sms, in the order they can happen.
OUTCOME_BAD_SIGNATURE = "bad_signature"      # the X-Twilio-Signature did not validate
OUTCOME_PROCESSING_OFF = "processing_off"    # PROCESSING_ENABLED is false
OUTCOME_PAUSED = "paused"                    # the storage switch is paused
OUTCOME_UNRECOGNIZED = "unrecognized"        # the body did not parse to a command
OUTCOME_NOT_FOUND = "not_found"              # no such request item
OUTCOME_ALREADY_DECIDED = "already_decided"  # the request was already processed
OUTCOME_NOT_REGISTERED = "not_registered"    # the sender's number is not in the directory
OUTCOME_NO_ACCESS = "no_access"              # the sender may not decide this request
OUTCOME_APPROVED = "approved"                # an approval went through
OUTCOME_REJECTED = "rejected"                # a rejection went through
OUTCOME_REFUSED = "refused"                  # the handler refused (a clash, already decided)
OUTCOME_ERROR = "error"                      # an unexpected exception


def only_digits(value: str | None) -> str:
    """Strip everything but the digits from a phone number.

    Args:
        value: A number in any format, or None.

    Returns:
        The digits only; "" for None or a value with no digits.
    """
    return re.sub(r"\D", "", value or "")                          # keep only 0-9


def last10(value: str | None) -> str:
    """The last ten digits of a number, the cross-format lookup key.

    The whole app matches a person to a text on the last ten digits (local
    number without the country code), so stored and queried numbers agree
    whatever their formatting.

    Args:
        value: A number in any format, or None.

    Returns:
        The last ten digits; a shorter run is returned whole.
    """
    return only_digits(value)[-10:]                                # last ten, or fewer if shorter


def as_utc(value: datetime | None) -> datetime | None:
    """Give a stored instant its UTC zone when the driver dropped it.

    Postgres returns these columns timezone-aware; the local SQLite fallback
    returns them naive. Both mean UTC.

    Args:
        value: A datetime read from a row, or None.

    Returns:
        The same instant, timezone-aware; None stays None.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class TwilioAnswer:
    """What one Twilio send answer amounted to, read off the JSON body.

    Returned by ``classify_twilio_response`` and stored by ``record_send``.
    """

    sid: str | None = None            # the message SID, status callbacks match on it
    status: str | None = None         # the message status (queued / sent / ...)
    error_code: str | None = None     # Twilio's numeric error code, when refused
    error_message: str | None = None  # Twilio's error message, when refused


def classify_twilio_response(http_status: int | None, response_text: str | None) -> TwilioAnswer:
    """Read a Twilio REST answer into a ``TwilioAnswer``. Pure; never raises.

    Twilio's success body is a Message resource with ``sid`` and ``status``
    (and null ``error_code`` / ``error_message``). An API error body instead
    carries a numeric ``code`` and a ``message`` and no ``sid``, plus a numeric
    ``status`` that is the HTTP status, not a message status, so a non-string
    ``status`` is ignored.

    Args:
        http_status: The HTTP status Twilio answered with (unused today, kept so
            the signature matches the email log's ``classify_response``).
        response_text: The response body text, or None.

    Returns:
        The answer, with whatever fields the body carried.
    """
    answer = TwilioAnswer()
    try:
        data = json.loads(response_text or "")                     # Twilio always answers JSON
    except (ValueError, TypeError):
        return answer                                              # non-JSON: nothing to read
    if not isinstance(data, dict):
        return answer
    answer.sid = data.get("sid")                                   # present only on a Message resource
    status = data.get("status")
    answer.status = status if isinstance(status, str) else None    # ignore the numeric status of an error body
    code = data.get("error_code", data.get("code"))                # success field, then the error field
    answer.error_code = str(code) if code is not None else None
    answer.error_message = data.get("error_message") or data.get("message")
    return answer


async def record_send(
    *,
    attempted_at: datetime,
    duration_ms: int | None,
    kind: str,
    to_number: str,
    from_number: str | None,
    body: str,
    request_kind: str | None = None,
    request_item_id: str | None = None,
    outbox_id: int | None = None,
    request_id: object | None = None,
    inbound_reply_id: int | None = None,
    http_status: int | None = None,
    response_body: str | None = None,
    no_response_reason: str | None = None,
    twilio_sid: str | None = None,
    twilio_status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write one ``sms_api_log`` row for a send attempt. Never raises.

    Called by ``send_sms`` on every exit path (success, HTTP error, network
    error, rehearsal skip, unparseable number). A write failure is swallowed so
    the send outcome the caller sees is never changed by a logging problem.

    Args:
        attempted_at: When the send was attempted (UTC).
        duration_ms: Round trip in milliseconds.
        kind: The text's kind (see the KIND_* constants / the outbox type).
        to_number: The recipient, in E.164 or the raw value for a bad number.
        from_number: Our Twilio sending number.
        body: The message body verbatim.
        request_kind: The request domain this text was about, when known.
        request_item_id: The request item id, when known.
        outbox_id: The outbox row this came from, when there was one.
        request_id: The request record id this came from, when there was one.
        inbound_reply_id: The inbound reply row this answers, for a reply send.
        http_status: Twilio's HTTP status, or None when it never answered.
        response_body: Twilio's body verbatim, SID already scrubbed out.
        no_response_reason: Why there is no HTTP answer (skip / bad number / error).
        twilio_sid: Twilio's message SID, read off the answer.
        twilio_status: Twilio's message status at send time.
        error_code: Twilio's error code, when the send was refused.
        error_message: Twilio's error message, when the send was refused.
    """
    try:
        async with async_session() as session:
            session.add(SmsApiLog(
                attempted_at=attempted_at,
                duration_ms=duration_ms,
                kind=kind or KIND_OTHER,
                request_kind=request_kind,
                request_item_id=str(request_item_id) if request_item_id is not None else None,
                outbox_id=outbox_id,
                request_id=str(request_id) if request_id is not None else None,
                inbound_reply_id=inbound_reply_id,
                to_number=to_number or "",
                to_digits=last10(to_number),                       # the lookup key
                from_number=from_number,
                body=body or "",
                http_status=http_status,
                response_body=response_body,
                no_response_reason=no_response_reason,
                twilio_sid=twilio_sid,
                twilio_status=twilio_status,
                error_code=error_code,
                error_message=error_message,
            ))
            await session.commit()
    except Exception:
        # Bookkeeping only: the send already happened and its outcome stands.
        logger.exception("Could not record SMS send (kind=%s)", kind)


async def record_reply(
    *,
    message_sid: str | None,
    from_number: str | None,
    to_number: str | None,
    body: str | None,
    signature_ok: bool,
) -> int | None:
    """Write the initial ``sms_reply_log`` row for an inbound text; return its id.

    Written up front, before the handler decides anything, so the instant reply
    the handler sends back can carry this row's id (``inbound_reply_id``).
    ``update_reply`` fills in the outcome and the parsed fields once they are
    known. Never raises.

    Args:
        message_sid: Twilio's inbound message SID.
        from_number: Who texted in (full).
        to_number: The Twilio number it arrived on.
        body: The inbound body verbatim.
        signature_ok: Whether the X-Twilio-Signature validated.

    Returns:
        The new row's id, or None when the write failed (the handler then simply
        logs no link, and still answers the sender normally).
    """
    try:
        async with async_session() as session:
            row = SmsReplyLog(
                message_sid=message_sid,
                from_number=from_number,
                from_digits=last10(from_number),                   # the lookup key
                to_number=to_number,
                body=body,
                signature_ok=signature_ok,
                outcome=OUTCOME_ERROR,                             # placeholder until update_reply runs
            )
            session.add(row)
            await session.commit()
            return row.id
    except Exception:
        logger.exception("Could not record inbound SMS reply (sid=%s)", message_sid)
        return None


async def update_reply(
    reply_id: int | None,
    *,
    outcome: str,
    decision: str | None = None,
    request_type: str | None = None,
    item_id: str | None = None,
    reason: str | None = None,
    sender_staff_id: str | None = None,
    error_text: str | None = None,
) -> None:
    """Fill in the outcome and parsed fields on an inbound reply row. Never raises.

    Args:
        reply_id: The row ``record_reply`` returned; None means it was never
            written, so there is nothing to update.
        outcome: The branch the handler took (an OUTCOME_* constant).
        decision: The parsed decision (Approve / Reject), when the body parsed.
        request_type: The parsed request domain, when the body parsed.
        item_id: The parsed request item id, when the body parsed.
        reason: A reject's free-text reason, when present.
        sender_staff_id: The matched sender's Staff Directory id, when found.
        error_text: A refusal's text or an exception's text, when relevant.
    """
    if reply_id is None:
        return                                                     # nothing was written to update
    try:
        async with async_session() as session:
            row = await session.get(SmsReplyLog, reply_id)
            if row is None:
                return                                             # vanished; nothing to do
            row.outcome = outcome
            row.decision = decision
            row.request_type = request_type
            row.item_id = str(item_id) if item_id is not None else None
            row.reason = reason
            row.sender_staff_id = str(sender_staff_id) if sender_staff_id is not None else None
            row.error_text = error_text
            await session.commit()
    except Exception:
        logger.exception("Could not update inbound SMS reply %s", reply_id)


async def apply_delivery_update(
    message_sid: str | None, status: str | None, error_code: str | None
) -> None:
    """Apply one Twilio delivery status callback to its send row. Never raises.

    Finds the send row by its message SID and updates the latest delivery
    status, the error code, the time, and appends a step to the JSON history. An
    unknown SID is a no-op (the message was not one of ours, or predates the
    log). Any DB problem is logged and swallowed so the webhook still answers 200.

    Args:
        message_sid: Twilio's ``MessageSid`` from the callback.
        status: Twilio's ``MessageStatus`` (sent / delivered / undelivered ...).
        error_code: Twilio's ``ErrorCode`` on an undelivered/failed callback.
    """
    if not message_sid:
        return                                                     # nothing to match on
    try:
        async with async_session() as session:
            stmt = (
                select(SmsApiLog)
                .where(SmsApiLog.twilio_sid == message_sid)
                .order_by(desc(SmsApiLog.id))                      # defensive: newest if ever duplicated
                .limit(1)
            )
            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                return                                             # unknown SID: do nothing, still 200
            now = _utcnow()
            row.delivery_status = status
            row.delivery_error_code = error_code
            row.delivery_updated_at = now
            history = _load_history(row.delivery_history)          # the steps so far
            history.append({"status": status, "error_code": error_code, "at": now.isoformat()})
            row.delivery_history = json.dumps(history)
            await session.commit()
    except Exception:
        logger.exception("Could not apply Twilio delivery update for sid %s", message_sid)


def _load_history(stored: str | None) -> list:
    """Parse the stored delivery history into a list, tolerating junk.

    Args:
        stored: The ``delivery_history`` JSON text, or None.

    Returns:
        The parsed list, or a fresh empty list when it is missing or unreadable.
    """
    if not stored:
        return []
    try:
        value = json.loads(stored)
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _utcnow() -> datetime:
    """UTC now, imported lazily so this module has no model-mixin import at top."""
    from app.models.mixins import utcnow
    return utcnow()


async def find_sends(
    *,
    digits: str | None = None,
    request_kind: str | None = None,
    item_id: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[SmsApiLog]:
    """Newest-first send rows for a number or a request, inside a window.

    Exactly one scope is used per call: ``digits`` matches the recipient's last
    ten digits; a ``request_kind`` + ``item_id`` pair matches the request the
    text was about. With neither, nothing is returned.

    Args:
        digits: Last ten digits of the number to match, or None.
        request_kind: Request domain to match (with ``item_id``), or None.
        item_id: Request item id to match (with ``request_kind``), or None.
        since: Only rows attempted at or after this instant, when given.
        limit: Maximum rows to return.

    Returns:
        Matching rows, newest first; empty when no scope was given or the query
        failed.
    """
    stmt = select(SmsApiLog)
    if digits:
        stmt = stmt.where(SmsApiLog.to_digits == last10(digits))
    elif request_kind and item_id:
        stmt = stmt.where(
            SmsApiLog.request_kind == request_kind,
            SmsApiLog.request_item_id == str(item_id),
        )
    else:
        return []                                                  # no scope: nothing to list
    if since is not None:
        stmt = stmt.where(SmsApiLog.attempted_at >= since)         # indexed window filter
    stmt = stmt.order_by(desc(SmsApiLog.attempted_at), desc(SmsApiLog.id)).limit(limit)
    try:
        async with async_session() as session:
            return list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.exception("SMS send-log query failed (digits=%s kind=%s)", bool(digits), request_kind)
        return []


async def find_replies(
    *,
    digits: str | None = None,
    request_kind: str | None = None,
    item_id: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[SmsReplyLog]:
    """Newest-first inbound reply rows for a number or a request, inside a window.

    ``digits`` matches the sender's last ten digits; a ``request_kind`` +
    ``item_id`` pair matches the request the reply named.

    Args:
        digits: Last ten digits of the sender's number to match, or None.
        request_kind: Request domain to match (with ``item_id``), or None.
        item_id: Request item id to match (with ``request_kind``), or None.
        since: Only rows received at or after this instant, when given.
        limit: Maximum rows to return.

    Returns:
        Matching rows, newest first; empty when no scope was given or the query
        failed.
    """
    stmt = select(SmsReplyLog)
    if digits:
        stmt = stmt.where(SmsReplyLog.from_digits == last10(digits))
    elif request_kind and item_id:
        stmt = stmt.where(
            SmsReplyLog.request_type == request_kind,
            SmsReplyLog.item_id == str(item_id),
        )
    else:
        return []
    if since is not None:
        stmt = stmt.where(SmsReplyLog.received_at >= since)
    stmt = stmt.order_by(desc(SmsReplyLog.received_at), desc(SmsReplyLog.id)).limit(limit)
    try:
        async with async_session() as session:
            return list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.exception("SMS reply-log query failed (digits=%s kind=%s)", bool(digits), request_kind)
        return []


async def find_recent_sends(
    *, since: datetime | None = None, limit: int = 200, text: str | None = None
) -> list[SmsApiLog]:
    """Newest-first send rows in a window, no scope, with an optional text filter.

    Backs the "all recent" view. The window on ``attempted_at`` (indexed) plus
    the limit bound the scan. ``text`` further narrows to rows whose body or
    recipient number contains it.

    Args:
        since: Only rows attempted at or after this instant, when given.
        limit: Hard cap on rows returned.
        text: Case-insensitive substring to match in the body or the number;
            blank or None means no text filter.

    Returns:
        Matching rows, newest first.

    Raises:
        Exception: The query failed. Not swallowed, so the admin page can show
            "lookup failed" instead of falsely claiming nothing was sent.
    """
    stmt = select(SmsApiLog)
    if since is not None:
        stmt = stmt.where(SmsApiLog.attempted_at >= since)
    needle = (text or "").strip().lower()
    if needle:
        # autoescape makes % and _ in the text literal, not SQL LIKE wildcards.
        body_match = func.lower(SmsApiLog.body).contains(needle, autoescape=True)
        number_match = SmsApiLog.to_digits.contains(only_digits(needle), autoescape=True) \
            if only_digits(needle) else body_match
        stmt = stmt.where(or_(body_match, number_match))
    stmt = stmt.order_by(desc(SmsApiLog.attempted_at), desc(SmsApiLog.id)).limit(limit)
    async with async_session() as session:
        return list((await session.execute(stmt)).scalars().all())


async def find_recent_replies(
    *, since: datetime | None = None, limit: int = 200, text: str | None = None
) -> list[SmsReplyLog]:
    """Newest-first inbound reply rows in a window, no scope, optional text filter.

    Args:
        since: Only rows received at or after this instant, when given.
        limit: Hard cap on rows returned.
        text: Case-insensitive substring to match in the body or the number.

    Returns:
        Matching rows, newest first.

    Raises:
        Exception: The query failed (not swallowed; see ``find_recent_sends``).
    """
    stmt = select(SmsReplyLog)
    if since is not None:
        stmt = stmt.where(SmsReplyLog.received_at >= since)
    needle = (text or "").strip().lower()
    if needle:
        body_match = func.lower(SmsReplyLog.body).contains(needle, autoescape=True)
        number_match = SmsReplyLog.from_digits.contains(only_digits(needle), autoescape=True) \
            if only_digits(needle) else body_match
        stmt = stmt.where(or_(body_match, number_match))
    stmt = stmt.order_by(desc(SmsReplyLog.received_at), desc(SmsReplyLog.id)).limit(limit)
    async with async_session() as session:
        return list((await session.execute(stmt)).scalars().all())


async def log_coverage_start() -> datetime | None:
    """When the log began: the earliest send or reply recorded.

    Shown so "nothing in the last 30 days" is read correctly while the table is
    younger than the window.

    Returns:
        The earliest instant across both tables, or None when both are empty or
        unreadable.
    """
    try:
        async with async_session() as session:
            send_min = (await session.execute(select(func.min(SmsApiLog.attempted_at)))).scalar()
            reply_min = (await session.execute(select(func.min(SmsReplyLog.received_at)))).scalar()
    except Exception:
        logger.exception("SMS log coverage query failed")
        return None
    candidates = [as_utc(v) for v in (send_min, reply_min) if v is not None]
    return min(candidates) if candidates else None
