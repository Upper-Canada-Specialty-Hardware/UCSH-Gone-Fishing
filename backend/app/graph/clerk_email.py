"""Client for Clerk transactional email.

Clerk sends mail to every non-UCSH (external) recipient once
``CLERK_EMAIL_ENABLED`` is on and ``CLERK_SECRET_KEY`` / ``CLERK_FROM_EMAIL``
are set. Clerk's email endpoint is experimental and takes one recipient per
call, so a send to several external addresses becomes several calls, each with
its own idempotency key and its own ``email_api_log`` row.

Three pieces, usable and testable on their own:

* ``build_payload`` is pure: the snake_case JSON for one recipient.
* ``classify_response`` is pure: Clerk's Email object read into the shared
  ``ExchangeSummary`` so the admin Email Log tab shows Clerk rows the same way.
* ``send`` makes one HTTP call with Clerk's retry rules, records one row in a
  ``finally`` (the writer never raises), and re-raises on an unrecovered
  failure like the SMTP2GO path.
"""

import asyncio
import json
import logging
import time

import httpx

from app.config import settings
from app.models.mixins import utcnow
from app.services.email_api_log import (
    OUTCOME_ACCEPTED,
    OUTCOME_HTTP_ERROR,
    OUTCOME_NO_RESPONSE,
    OUTCOME_REJECTED,
    OUTCOME_UNREADABLE,
    RESPONSE_MAX_CHARS,
    ExchangeSummary,
    record_exchange,
)

logger = logging.getLogger(__name__)

# One AsyncClient for the module, like the other paths; tests monkeypatch it.
_http = httpx.AsyncClient(timeout=30.0)
# Indirection so tests can replace the sleep between retries with a no-op.
_sleep = asyncio.sleep

# The email endpoint, appended to the base CLERK_API_URL.
SEND_PATH = "/email"
# At most three tries for a retryable failure, counting the first attempt.
MAX_ATTEMPTS = 3
# Never wait longer than this for a 429's Retry-After.
RETRY_AFTER_CAP_SECONDS = 30
# Fallback wait when a retryable answer carries no Retry-After (5xx).
DEFAULT_RETRY_WAIT_SECONDS = 1.0


def send_url() -> str:
    """Full email url for Clerk.

    Returns:
        ``CLERK_API_URL`` with any trailing slash removed plus ``/email``.
    """
    return settings.CLERK_API_URL.rstrip("/") + SEND_PATH


def is_configured() -> bool:
    """Whether Clerk has the settings it needs to be called.

    Returns:
        True when both ``CLERK_SECRET_KEY`` and ``CLERK_FROM_EMAIL`` are set.
    """
    return bool(settings.CLERK_SECRET_KEY and settings.CLERK_FROM_EMAIL)


def build_payload(address: str, subject: str, html: str) -> dict:
    """The snake_case JSON body posted to Clerk for one recipient.

    Args:
        address: The single recipient address.
        subject: Email subject (Clerk caps this at 998 chars).
        html: Full HTML body (Clerk caps html+text at 50,000 bytes).

    Returns:
        The payload dict. The secret key is a header, never in the body. A
        reply-to is added only when ``CLERK_REPLY_TO`` is set.
    """
    payload: dict = {
        "to": {"address": address},                                # exactly one recipient
        "from": {"address": settings.CLERK_FROM_EMAIL},            # verified production sender
        "subject": subject,
        "html": html,
    }
    if settings.CLERK_REPLY_TO:                                    # optional, same domain
        payload["reply_to"] = {"address": settings.CLERK_REPLY_TO}
    return payload


def _should_retry(status: int) -> bool:
    """Whether this status is one of Clerk's retryable failures.

    Args:
        status: HTTP status Clerk returned.

    Returns:
        True for 429 and any 5xx.
    """
    return status == 429 or status >= 500


def _retry_wait(status: int, response: httpx.Response) -> float:
    """How long to wait before the next attempt.

    Args:
        status: HTTP status Clerk returned.
        response: The response, read for a 429's Retry-After header.

    Returns:
        Seconds to sleep: the capped Retry-After for a 429, else a short
        fixed backoff.
    """
    if status == 429:                                              # honour the server's pace
        raw = response.headers.get("Retry-After")
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            seconds = DEFAULT_RETRY_WAIT_SECONDS                   # missing/garbled header
        return min(seconds, RETRY_AFTER_CAP_SECONDS)
    return DEFAULT_RETRY_WAIT_SECONDS                              # 5xx


def classify_response(
    http_status: int | None,
    response_body: str | None,
    no_response_reason: str | None = None,
) -> ExchangeSummary:
    """Read Clerk's answer into an ``ExchangeSummary``. Pure; never raises.

    A success returns an Email object carrying ``id``, ``status``,
    ``to_email_address``, ``delivered_by_clerk`` and ``suppression_reason``.
    One recipient per call, so a readable success is one accepted recipient,
    unless a ``suppression_reason`` is present (the address is on Clerk's
    suppression list), in which case it is counted as rejected.

    Args:
        http_status: Status Clerk answered with; None when it never did.
        response_body: Body text as received; None when it never answered.
        no_response_reason: Exception text when there was no answer.

    Returns:
        The summary, with the id and counts filled in on a readable success.
    """
    if http_status is None:                                        # timeout, DNS, refused
        return ExchangeSummary(
            outcome=OUTCOME_NO_RESPONSE, no_response_reason=no_response_reason
        )
    summary = ExchangeSummary(
        outcome=OUTCOME_UNREADABLE, http_status=http_status, response_body=response_body
    )
    if http_status >= 400:                                         # an error body, kept verbatim
        summary.outcome = OUTCOME_HTTP_ERROR
        return summary
    try:
        body = json.loads(response_body or "")
        summary.email_id = body.get("id")                          # Clerk's email id
        if body.get("suppression_reason"):                         # address suppressed: not delivered
            summary.outcome = OUTCOME_REJECTED
            summary.succeeded = 0
            summary.failed = 1
        elif summary.email_id is not None:                         # an Email object came back
            summary.outcome = OUTCOME_ACCEPTED
            summary.succeeded = 1
            summary.failed = 0
    except (ValueError, AttributeError, TypeError):
        pass                                                       # keep the raw body; stays unreadable
    return summary


async def send(
    address: str, field: str, subject: str, html: str, idempotency_key: str
) -> ExchangeSummary:
    """Send one external email through Clerk and record the exchange.

    Exactly one ``email_api_log`` row is written, in a ``finally``, holding the
    final answer after any retries. Clerk retries 429 (waiting the capped
    Retry-After) and any 5xx with the same key, at most three attempts in all.
    An unrecovered 4xx/5xx raises after the row is written, like the SMTP2GO
    path.

    Args:
        address: The single recipient address.
        field: "to" or "cc" in the original send; stored so the admin lookup
            shows where the address sat, even though Clerk has no CC.
        subject: Email subject.
        html: Full HTML body.
        idempotency_key: Stable key reused across this recipient's retries.

    Returns:
        The ``ExchangeSummary`` read off Clerk's final answer.

    Raises:
        httpx.HTTPStatusError: Clerk's last answer was 4xx/5xx.
        httpx.HTTPError: The request never completed.
    """
    payload = build_payload(address, subject, html)                # body (no secret inside)
    url = send_url()
    headers = {                                                    # key and idempotency go in headers
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
        "Idempotency-Key": idempotency_key,
    }
    attempted_at = utcnow()                                        # when we first asked
    started = time.monotonic()                                     # for the round-trip time
    http_status: int | None = None
    response_body: str | None = None
    no_response_reason: str | None = None
    resp: httpx.Response | None = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):                 # first attempt plus retries
            resp = await _http.post(url, json=payload, headers=headers)
            http_status = resp.status_code                         # answered, whatever the status
            response_body = resp.text[:RESPONSE_MAX_CHARS]         # the answer, verbatim
            if attempt < MAX_ATTEMPTS and _should_retry(http_status):
                await _sleep(_retry_wait(http_status, resp))       # back off, same key, try again
                continue
            break                                                  # success, or a non-retryable answer
    except Exception as e:                                         # timeout, DNS, refused, bad TLS
        no_response_reason = f"{type(e).__name__}: {e}"
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = classify_response(http_status, response_body, no_response_reason)
        await record_exchange(                                     # never raises
            summary,
            request_url=url,
            payload=payload,
            attempted_at=attempted_at,
            duration_ms=duration_ms,
            sender=settings.CLERK_FROM_EMAIL,
            subject=subject,
            recipient_source={field: [address]},                   # flat list for the lookup join
        )
    if resp is not None and resp.status_code >= 400:               # unrecovered error: log and raise
        logger.error("Clerk email %d: %s", resp.status_code, (response_body or "")[:500])
        resp.raise_for_status()
    logger.info(
        "Email sent via Clerk to %s - subject: %s (%s, id %s)",
        address, subject, summary.outcome, summary.email_id,
    )
    return summary
