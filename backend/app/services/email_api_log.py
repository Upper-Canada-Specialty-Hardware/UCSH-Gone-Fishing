"""Record and query every SMTP2GO API exchange.

``app/graph/email.py`` calls ``record_exchange`` once per send attempt, whatever
happened, and the admin email-log endpoint calls ``find_exchanges`` to answer
"what did the backend ask SMTP2GO to send this person, and what did SMTP2GO
answer". Everything here is bookkeeping around a send that has already
happened: a failure to write or read a row is logged and swallowed, and never
changes the outcome the caller sees.

Two halves, kept apart on purpose:

* ``classify_response`` is pure. It turns an HTTP status and a response body
  into an ``ExchangeSummary`` and never touches the database, so the email
  client can hand the summary back to its caller even when logging fails.
* ``record_exchange`` persists a summary plus the redacted request.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, func, select

from app.database import async_session
from app.models import EmailApiLog, EmailApiLogRecipient

logger = logging.getLogger(__name__)

# Outcome of one call, derived from the HTTP status and the response body.
OUTCOME_ACCEPTED = "accepted"                      # 200, failed == 0, succeeded >= 1
OUTCOME_PARTIALLY_ACCEPTED = "partially_accepted"  # 200, some recipients failed
OUTCOME_REJECTED = "rejected"                      # 200, every recipient failed
OUTCOME_HTTP_ERROR = "http_error"                  # 4xx / 5xx
OUTCOME_UNREADABLE = "unreadable_response"         # 2xx, but not the documented JSON
OUTCOME_NO_RESPONSE = "no_response"                # request never completed
OUTCOME_NOT_ATTEMPTED = "not_attempted"            # never called: no usable recipient

# A documented response is a few hundred bytes. The cap only stops an
# unexpected HTML error page from bloating a row.
RESPONSE_MAX_CHARS = 8000

# Default lookup window: the admin question is "the past 30 days", and thirty
# days also matches the dashboard link lifetime and the first reminder.
DEFAULT_WINDOW_DAYS = 30

# Recipient fields of the SMTP2GO payload, in the order they are recorded.
RECIPIENT_FIELDS = ("to", "cc")


@dataclass
class ExchangeSummary:
    """What one SMTP2GO call amounted to, read off the answer.

    Returned by ``send_email`` so a caller can act on a rejection, and stored
    by ``record_exchange`` so an admin can read it later.
    """

    outcome: str
    http_status: int | None = None
    response_body: str | None = None
    no_response_reason: str | None = None
    succeeded: int | None = None
    failed: int | None = None
    email_id: str | None = None
    request_id: str | None = None


def normalize_address(address: str | None) -> str:
    """Lower-case and strip an email address so stored and queried forms agree.

    Args:
        address: Raw address from the Staff Directory or typed by an admin.

    Returns:
        The normalised address, or "" for None or blank.
    """
    return (address or "").strip().lower()


def as_utc(value: datetime | None) -> datetime | None:
    """Give a stored instant its UTC zone when the driver dropped it.

    The columns are written timezone-aware, and Postgres returns them that
    way. The local SQLite fallback returns them naive. Both mean UTC.

    Args:
        value: A datetime read from the log, or None.

    Returns:
        The same instant, timezone-aware; None stays None.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def redact_request(payload: dict) -> dict:
    """Copy of the SMTP2GO payload that is safe to store.

    Args:
        payload: The exact JSON body posted to SMTP2GO.

    Returns:
        The payload without ``api_key``, and with ``html_body`` replaced by
        ``html_body_bytes`` and ``html_body_sha256``. Everything else, the
        recipients included, is copied as sent.
    """
    safe = {k: v for k, v in payload.items() if k not in ("api_key", "html_body")}
    body = payload.get("html_body")
    if body is not None:
        raw = body.encode("utf-8")                                   # size and hash of the bytes sent
        safe["html_body_bytes"] = len(raw)
        safe["html_body_sha256"] = hashlib.sha256(raw).hexdigest()
    return safe


def classify_response(
    http_status: int | None,
    response_body: str | None,
    no_response_reason: str | None = None,
) -> ExchangeSummary:
    """Read SMTP2GO's answer into an ``ExchangeSummary``. Pure; never raises.

    SMTP2GO documents that ``/email/send`` answers 200 even when recipients
    were rejected, and that the caller must read ``data.failed`` and
    ``data.failures``. ``data.succeeded`` counts recipients accepted into
    their queue, which is not delivery.

    Args:
        http_status: Status SMTP2GO answered with; None when it never answered.
        response_body: Body text as received; None when it never answered.
        no_response_reason: Exception text when there was no answer.

    Returns:
        The summary, with the counts and ids filled in when the body is the
        documented JSON shape.
    """
    if http_status is None:
        return ExchangeSummary(
            outcome=OUTCOME_NO_RESPONSE, no_response_reason=no_response_reason
        )
    summary = ExchangeSummary(
        outcome=OUTCOME_UNREADABLE, http_status=http_status, response_body=response_body
    )
    try:
        body = json.loads(response_body or "")
        data = body.get("data") or {}
        summary.request_id = body.get("request_id")                  # present on every answer
        summary.email_id = data.get("email_id")                      # present when something was queued
        succeeded = data.get("succeeded")
        failed = data.get("failed")
        summary.succeeded = int(succeeded) if succeeded is not None else None
        summary.failed = int(failed) if failed is not None else None
    except (ValueError, AttributeError, TypeError):
        pass                                                         # keep the raw body; stays unreadable
    if http_status >= 400:
        summary.outcome = OUTCOME_HTTP_ERROR                         # even if the body parsed
    elif summary.succeeded is None and summary.failed is None:
        summary.outcome = OUTCOME_UNREADABLE                         # 2xx without the documented counts
    elif (summary.failed or 0) == 0 and (summary.succeeded or 0) > 0:
        summary.outcome = OUTCOME_ACCEPTED
    elif (summary.succeeded or 0) > 0:
        summary.outcome = OUTCOME_PARTIALLY_ACCEPTED
    else:
        summary.outcome = OUTCOME_REJECTED                           # nothing queued, on a 200
    return summary


def _recipient_rows(payload: dict) -> list[EmailApiLogRecipient]:
    """One recipient row per usable address in the payload's To and CC.

    Args:
        payload: The SMTP2GO payload (redacted or not; only recipients are read).

    Returns:
        Recipient rows with normalised addresses; blanks are dropped.
    """
    rows = []
    for field in RECIPIENT_FIELDS:
        for raw in payload.get(field) or []:
            address = normalize_address(raw)
            if address:
                rows.append(EmailApiLogRecipient(address=address, field=field))
    return rows


async def record_exchange(
    summary: ExchangeSummary,
    *,
    request_url: str,
    payload: dict,
    attempted_at: datetime,
    duration_ms: int | None,
) -> None:
    """Write one email_api_log row and its recipient rows. Never raises.

    Args:
        summary: The classified answer (or the reason there is none).
        request_url: Endpoint the request went to.
        payload: The exact payload posted; redacted here before storage.
        attempted_at: When the request was made (UTC).
        duration_ms: Round trip in milliseconds; None when never called.
    """
    try:
        safe = redact_request(payload)
        async with async_session() as session:
            session.add(EmailApiLog(
                attempted_at=attempted_at,
                duration_ms=duration_ms,
                request_url=request_url,
                sender=str(payload.get("sender") or ""),
                subject=str(payload.get("subject") or ""),
                request_json=json.dumps(safe, ensure_ascii=False),  # verbatim minus secrets
                http_status=summary.http_status,
                response_body=summary.response_body,
                no_response_reason=summary.no_response_reason,
                outcome=summary.outcome,
                succeeded_count=summary.succeeded,
                failed_count=summary.failed,
                smtp2go_email_id=summary.email_id,
                smtp2go_request_id=summary.request_id,
                recipients=_recipient_rows(payload),               # cascades with the log row
            ))
            await session.commit()
    except Exception:
        # Bookkeeping only: the send outcome is already decided and the caller
        # must see that outcome, not a database problem.
        logger.exception(
            "Could not record SMTP2GO exchange for subject %r", payload.get("subject")
        )


async def find_exchanges(
    *,
    address: str | None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[EmailApiLog]:
    """Newest-first SMTP2GO calls that named an address in To or CC.

    Args:
        address: Email address to match; normalised here.
        since: Only calls made at or after this instant, when given.
        limit: Maximum rows to return.

    Returns:
        Matching rows with recipients loaded, newest first. Empty when no
        address was given or the query itself fails.
    """
    needle = normalize_address(address)
    if not needle:
        return []
    # Subquery on the indexed recipient table; IN keeps a row that carries the
    # address in both To and CC from appearing twice.
    matching_ids = select(EmailApiLogRecipient.log_id).where(
        EmailApiLogRecipient.address == needle
    )
    stmt = select(EmailApiLog).where(EmailApiLog.id.in_(matching_ids))
    if since is not None:
        stmt = stmt.where(EmailApiLog.attempted_at >= since)        # window filter
    stmt = stmt.order_by(desc(EmailApiLog.attempted_at), desc(EmailApiLog.id)).limit(limit)
    try:
        async with async_session() as session:
            return list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.exception("Email log query failed for %s", needle)
        return []


async def log_coverage_start() -> datetime | None:
    """When the log began: the earliest recorded call.

    The admin page shows this so "nothing in the last 30 days" is read
    correctly while the table is younger than 30 days.

    Returns:
        The earliest ``attempted_at``, or None when the table is empty or
        unreadable.
    """
    try:
        async with async_session() as session:
            result = await session.execute(select(func.min(EmailApiLog.attempted_at)))
            return as_utc(result.scalar())
    except Exception:
        logger.exception("Email log coverage query failed")
        return None
