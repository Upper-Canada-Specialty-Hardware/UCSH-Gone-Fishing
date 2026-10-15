"""Record and query every outbound email exchange, for both services.

``app/graph/email.py`` (SMTP2GO) and ``app/graph/mailer_client.py`` (the UCSH
mailer, which relays internal mail over Microsoft HVE) each call
``record_exchange`` once per send attempt, whatever happened. The admin
email-log endpoints read it back: ``find_exchanges`` answers "what did the
backend ask to send this person, and what did the service answer", and
``find_recent`` lists every recent send across both services. Everything here is
bookkeeping around a send that has already happened: a failure to write a row is
logged and swallowed, and never changes the outcome the caller sees.

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

from sqlalchemy import desc, func, or_, select

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

# A send is a likely duplicate when the same subject reached the same recipient
# again within this many seconds. Five minutes catches the real case (one
# request firing the same approval email at a manager twice) without flagging a
# genuine resend days later.
DUPLICATE_WINDOW_SECONDS = 300

# Recipient fields of a payload, in the order they are recorded.
RECIPIENT_FIELDS = ("to", "cc")

# Payload keys that carry a secret and must never be stored. SMTP2GO puts its
# key in the body (api_key); the UCSH mailer puts its key in the
# Authorization header, which is never handed to the recorder, so these extra
# names are belt-and-braces only.
_SECRET_KEYS = ("api_key", "authorization", "bearer_token")
# Payload keys that carry a message body: stored as a byte length and a
# SHA-256 only, never the text, because bodies carry signed approval links and
# this table is read by an unauthenticated admin endpoint. "html_body" is the
# SMTP2GO field; "html" and "text" are the UCSH mailer's fields.
_BODY_KEYS = ("html_body", "html", "text")

# Which email service a stored row went to, derived from its request url so no
# column is needed. The admin Email Log tab labels each row with this.
SERVICE_SMTP2GO = "smtp2go"
SERVICE_UCSH_MAILER = "ucsh_mailer"
SERVICE_UNKNOWN = "unknown"


def service_for_url(request_url: str | None) -> str:
    """Name the email service a logged exchange went to, from its url.

    Deriving the service from the stored ``request_url`` keeps the
    providers distinguishable on the admin page without adding a column.

    Args:
        request_url: The endpoint the request went to, as stored on the row.

    Returns:
        One of the ``SERVICE_*`` constants. ``SERVICE_UNKNOWN`` when the url
        matches none of them (e.g. an operator set an odd mailer url).
    """
    url = (request_url or "").lower()                              # tolerate None
    if "smtp2go" in url:                                           # api.smtp2go.com
        return SERVICE_SMTP2GO
    if url.endswith("/v1/send"):                                   # the UCSH mailer's send path
        return SERVICE_UCSH_MAILER
    return SERVICE_UNKNOWN


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
    """Copy of an email payload that is safe to store.

    Works for both providers. The SMTP2GO payload (``api_key`` +
    ``html_body``) comes out exactly as before; the UCSH mailer payload
    (``html`` / ``text`` bodies, key in a header) is redacted the same way.

    Args:
        payload: The exact JSON body posted to the email service.

    Returns:
        The payload with every secret key dropped and every body field
        replaced by ``<field>_bytes`` and ``<field>_sha256``. Everything else,
        the recipients included, is copied as sent.
    """
    dropped = _SECRET_KEYS + _BODY_KEYS                              # keys not copied verbatim
    safe = {k: v for k, v in payload.items() if k not in dropped}   # keep the rest unchanged
    for field in _BODY_KEYS:                                        # summarise each body present
        body = payload.get(field)
        if isinstance(body, str):                                   # only real text bodies
            raw = body.encode("utf-8")                              # size and hash of the bytes sent
            safe[f"{field}_bytes"] = len(raw)
            safe[f"{field}_sha256"] = hashlib.sha256(raw).hexdigest()
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


def combine_summaries(summaries: list[ExchangeSummary]) -> ExchangeSummary:
    """Fold one send's per-call summaries into a single whole-send summary.

    A send can now be split across two services (and the mailer's refusal
    fallback can add a call), so ``send_email`` collects several summaries and
    returns one. A single summary is returned unchanged, so a send that touches only
    one service (the default, SMTP2GO-only) returns exactly what that one call
    produced and nothing about the existing behaviour shifts.

    For several calls the counts are summed, the first id seen is kept, and
    ``http_status``/``response_body`` are left empty because each call already
    has its own row holding its verbatim answer. The combined outcome is:
    ``accepted`` when every call was accepted, ``partially_accepted`` when some
    succeeded and some did not, ``rejected`` when none succeeded, and
    ``not_attempted`` when nothing was sent.

    Args:
        summaries: One summary per call made for a single ``send_email``.

    Returns:
        The whole-send summary.
    """
    if not summaries:                                              # nothing was sent at all
        return ExchangeSummary(outcome=OUTCOME_NOT_ATTEMPTED)
    if len(summaries) == 1:                                        # one service: unchanged behaviour
        return summaries[0]
    succeeded_vals = [s.succeeded for s in summaries if s.succeeded is not None]
    failed_vals = [s.failed for s in summaries if s.failed is not None]
    succeeded = sum(succeeded_vals) if succeeded_vals else None    # None only when nobody reported
    failed = sum(failed_vals) if failed_vals else None
    outcomes = [s.outcome for s in summaries]
    any_ok = any(o in (OUTCOME_ACCEPTED, OUTCOME_PARTIALLY_ACCEPTED) for o in outcomes)
    any_bad = any(
        o in (OUTCOME_REJECTED, OUTCOME_HTTP_ERROR, OUTCOME_NO_RESPONSE, OUTCOME_UNREADABLE)
        for o in outcomes
    )
    if all(o == OUTCOME_NOT_ATTEMPTED for o in outcomes):          # no routable recipient anywhere
        outcome = OUTCOME_NOT_ATTEMPTED
    elif any_ok and any_bad:                                       # a mix: some out, some not
        outcome = OUTCOME_PARTIALLY_ACCEPTED
    elif any_ok:                                                   # everything that went, went
        outcome = OUTCOME_ACCEPTED
    else:                                                          # nothing succeeded
        outcome = OUTCOME_REJECTED
    return ExchangeSummary(
        outcome=outcome,
        succeeded=succeeded,
        failed=failed,
        email_id=next((s.email_id for s in summaries if s.email_id), None),
        request_id=next((s.request_id for s in summaries if s.request_id), None),
        no_response_reason=next((s.no_response_reason for s in summaries if s.no_response_reason), None),
    )


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
    sender: str | None = None,
    subject: str | None = None,
) -> None:
    """Write one email_api_log row and its recipient rows. Never raises.

    One row per logical call, whatever the provider. The SMTP2GO path calls
    this with just the payload (sender/subject read off it, recipients read
    off its ``to``/``cc`` lists). The UCSH mailer path passes
    ``sender``/``subject`` explicitly, because its payload renames those.

    Args:
        summary: The classified answer (or the reason there is none).
        request_url: Endpoint the request went to; also names the service.
        payload: The exact payload posted; redacted here before storage.
        attempted_at: When the request was made (UTC).
        duration_ms: Round trip in milliseconds; None when never called.
        sender: Sender to store; falls back to ``payload["sender"]`` when None.
        subject: Subject to store; falls back to ``payload["subject"]``.
    """
    try:
        safe = redact_request(payload)
        rows = _recipient_rows(payload)
        async with async_session() as session:
            session.add(EmailApiLog(
                attempted_at=attempted_at,
                duration_ms=duration_ms,
                request_url=request_url,
                sender=str(sender if sender is not None else (payload.get("sender") or "")),
                subject=str(subject if subject is not None else (payload.get("subject") or "")),
                request_json=json.dumps(safe, ensure_ascii=False),  # verbatim minus secrets
                http_status=summary.http_status,
                response_body=summary.response_body,
                no_response_reason=summary.no_response_reason,
                outcome=summary.outcome,
                succeeded_count=summary.succeeded,
                failed_count=summary.failed,
                smtp2go_email_id=summary.email_id,
                smtp2go_request_id=summary.request_id,
                recipients=rows,                                   # cascades with the log row
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
    """Newest-first calls (either service) that named an address in To or CC.

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


async def find_recent(
    *,
    since: datetime | None = None,
    limit: int = 200,
    text: str | None = None,
) -> list[EmailApiLog]:
    """Newest-first calls to any service in a window, no person needed.

    This backs the "every recent email" view: unlike ``find_exchanges`` it is
    not scoped to one address, so the primary filter is the window on
    ``attempted_at`` (an indexed column), which keeps the scan bounded. An
    optional ``text`` further narrows the window to rows whose subject or one of
    whose recipient addresses contains it.

    Args:
        since: Only calls made at or after this instant, when given.
        limit: Hard cap on rows returned; the window plus this cap bound the work.
        text: Case-insensitive substring to match in the subject or a recipient
            address; blank or None means no text filter.

    Returns:
        Matching rows with recipients loaded, newest first.

    Raises:
        Exception: The underlying query failed. Unlike ``find_exchanges`` this
            does not swallow the error into an empty list, so the admin page can
            show "lookup failed" instead of falsely claiming nothing was sent.
    """
    stmt = select(EmailApiLog)
    if since is not None:
        stmt = stmt.where(EmailApiLog.attempted_at >= since)        # indexed window filter first
    needle = (text or "").strip().lower()                           # addresses are stored lower-cased
    if needle:
        # contains() with autoescape treats % and _ in the text as literals, so
        # a subject or address containing them cannot act as a wildcard.
        subject_match = func.lower(EmailApiLog.subject).contains(needle, autoescape=True)
        # Correlated EXISTS on the child table: tied to this row's id, so the
        # attempted_at window above still bounds which rows are probed.
        address_match = (
            select(EmailApiLogRecipient.id)
            .where(EmailApiLogRecipient.log_id == EmailApiLog.id)
            .where(EmailApiLogRecipient.address.contains(needle, autoescape=True))
            .exists()
        )
        stmt = stmt.where(or_(subject_match, address_match))
    stmt = stmt.order_by(desc(EmailApiLog.attempted_at), desc(EmailApiLog.id)).limit(limit)
    async with async_session() as session:
        return list((await session.execute(stmt)).scalars().all())


# Only a send that actually went out can be a real duplicate. A refused mailer
# call, its resend and the SMTP2GO fallback all share one subject within seconds
# (graph/email.py), and a failed-then-retried send repeats too; none of those is
# a double-delivery, so only these outcomes are bucketed.
_DELIVERED_OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_PARTIALLY_ACCEPTED)


def find_duplicate_ids(rows: list[EmailApiLog]) -> set[int]:
    """Ids of rows that look like a duplicate send within the given set. Pure.

    A row is flagged when another row in ``rows`` carried the same subject to
    the same recipient address within ``DUPLICATE_WINDOW_SECONDS``. Both rows of
    a close pair are flagged, so the admin page can mark every member of a burst.
    Only rows whose outcome is accepted or partially accepted are considered: a
    refused mailer call followed by its resend and fallback, or a failed call
    then its retry, are not double-deliveries and must not be flagged. The check
    is confined to the rows passed in (one window, or one person), so it never
    queries the database and never raises.

    Args:
        rows: The exchanges under consideration, recipients loaded.

    Returns:
        The set of ``EmailApiLog.id`` values that are likely duplicates; empty
        when nothing repeats.
    """
    # (subject, recipient address) -> {row id: when}, one entry per row so an
    # address that is in both To and CC of one row cannot pair with itself.
    buckets: dict[tuple[str, str], dict[int, datetime]] = {}
    for row in rows:
        if row.outcome not in _DELIVERED_OUTCOMES:                  # only real deliveries can duplicate
            continue
        when = as_utc(row.attempted_at)                             # compare in a single zone
        if when is None:                                            # undated row cannot be placed
            continue
        for recipient in row.recipients:
            buckets.setdefault((row.subject, recipient.address), {})[row.id] = when
    duplicates: set[int] = set()
    for by_id in buckets.values():
        if len(by_id) < 2:                                          # one send to this pair: nothing to compare
            continue
        ordered = sorted(by_id.items(), key=lambda item: item[1])  # by time, oldest first
        for (prev_id, prev_when), (this_id, this_when) in zip(ordered, ordered[1:]):
            if (this_when - prev_when).total_seconds() <= DUPLICATE_WINDOW_SECONDS:
                duplicates.add(prev_id)                             # both ends of a close pair
                duplicates.add(this_id)
    return duplicates


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
