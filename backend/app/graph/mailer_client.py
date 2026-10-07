"""Client for the UCSH mailer.

The UCSH mailer is an internal HTTPS service that relays mail over Microsoft
High Volume Email. It is reached only for UCSH-domain (internal) recipients,
and only once ``MAILER_ENABLED`` is on and ``MAILER_URL`` / ``MAILER_KEY`` are
set. Authentication is a single static per-product bearer key, so there is no
sign-in step.

Three pieces are used separately so each can be read and tested on its own:

* ``build_payload`` is pure: it builds the exact camelCase JSON the mailer
  expects, including the rule about when a CC may ride along.
* ``classify_response`` is pure: it reads the mailer's answer into the same
  ``ExchangeSummary`` the SMTP2GO path produces, so the admin Email Log tab
  shows mailer rows the same way.
* ``send`` makes the HTTP call with the mailer's retry rules, records exactly
  one ``email_api_log`` row in a ``finally`` (the writer never raises), and
  re-raises on an unrecovered failure the way the SMTP2GO path does.
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
    OUTCOME_UNREADABLE,
    RESPONSE_MAX_CHARS,
    ExchangeSummary,
    record_exchange,
)

logger = logging.getLogger(__name__)

# One AsyncClient for the module, like the SMTP2GO path; tests monkeypatch it.
_http = httpx.AsyncClient(timeout=30.0)
# Indirection so tests can replace the sleep between retries with a no-op.
_sleep = asyncio.sleep

# The send endpoint, appended to the operator-set base MAILER_URL.
SEND_PATH = "/v1/send"
# The mailer splits a message above this many recipients; a CC is only allowed
# on a single-message send, so above it the CC addresses move into "to".
MAX_RECIPIENTS_PER_MESSAGE = 50
# At most three tries for a retryable failure, counting the first attempt.
MAX_ATTEMPTS = 3
# Never wait longer than this for a 429's Retry-After, so a huge value cannot
# stall a request indefinitely.
RETRY_AFTER_CAP_SECONDS = 30
# Fallback wait when a retryable answer carries no Retry-After (503, and the
# one retryable 502).
DEFAULT_RETRY_WAIT_SECONDS = 1.0
# The only 502 error code worth retrying: a transient HVE token refresh.
RETRYABLE_502_ERROR = "hve_token_failed"


def send_url() -> str:
    """Full send url for this mailer.

    Returns:
        ``MAILER_URL`` with any trailing slash removed plus ``/v1/send``.
    """
    return settings.MAILER_URL.rstrip("/") + SEND_PATH


def is_configured() -> bool:
    """Whether the mailer has the settings it needs to be called.

    Returns:
        True when both ``MAILER_URL`` and ``MAILER_KEY`` are set. The sender is
        optional, because a single-sender key supplies its own.
    """
    return bool(settings.MAILER_URL and settings.MAILER_KEY)


def build_payload(to: list[str], cc: list[str], subject: str, html: str) -> dict:
    """The camelCase JSON body posted to the mailer.

    The mailer refuses unknown fields, so only documented keys are included.
    The CC rule: when the whole send fits in one message (<= 50 recipients) the
    CC addresses stay in ``cc``; above that the mailer splits into several
    messages, which may not carry a CC, so the CC addresses are folded into
    ``to`` instead (they still receive the mail, just not as visible CC).

    Args:
        to: Internal recipient addresses, blanks already removed.
        cc: Internal CC addresses, blanks already removed.
        subject: Email subject (required by the mailer).
        html: Full HTML body (one of html/text is required; this app sends HTML).

    Returns:
        The payload dict. The bearer key is a header, never in the body.
    """
    total = len(to) + len(cc)                                      # decides the CC rule
    if cc and total > MAX_RECIPIENTS_PER_MESSAGE:                  # multi-message: CC not allowed
        recipients_to = list(to) + list(cc)                       # fold CC into to
        recipients_cc: list[str] = []
    else:                                                          # single message: CC may ride along
        recipients_to = list(to)
        recipients_cc = list(cc)
    payload: dict = {"to": recipients_to, "subject": subject, "html": html}
    if recipients_cc:                                             # omit empty cc entirely
        payload["cc"] = recipients_cc
    if settings.MAILER_FROM:                                      # only needed for multi-sender keys
        payload["from"] = settings.MAILER_FROM
    if settings.MAILER_FROM_NAME:                                 # display name for staff recipients
        payload["fromName"] = settings.MAILER_FROM_NAME
    return payload


def _error_code(response_body: str | None) -> str | None:
    """Mailer error code from an error body, or None when it is not readable.

    Args:
        response_body: The raw response text.

    Returns:
        The ``error`` field, or None when the body is not the documented JSON.
    """
    try:
        return (json.loads(response_body or "") or {}).get("error")
    except (ValueError, AttributeError, TypeError):
        return None


def _should_retry(status: int, response_body: str | None) -> bool:
    """Whether this answer is one of the mailer's retryable failures.

    Args:
        status: HTTP status the mailer returned.
        response_body: Raw response text, read for the 502 error code.

    Returns:
        True for 429, 503, and a 502 whose error is ``hve_token_failed``.
    """
    if status in (429, 503):                                       # rate limited / temporarily down
        return True
    if status == 502:                                              # only the token-refresh 502 retries
        return _error_code(response_body) == RETRYABLE_502_ERROR
    return False


def _retry_wait(status: int, response: httpx.Response) -> float:
    """How long to wait before the next attempt.

    Args:
        status: HTTP status the mailer returned.
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
        return min(seconds, RETRY_AFTER_CAP_SECONDS)               # never wait too long
    return DEFAULT_RETRY_WAIT_SECONDS                              # 503 / hve_token_failed


def classify_response(
    http_status: int | None,
    response_body: str | None,
    no_response_reason: str | None = None,
) -> ExchangeSummary:
    """Read the mailer's answer into an ``ExchangeSummary``. Pure; never raises.

    A 200 carries ``status`` ("sent" or "logged"), a ``recipients`` count, and
    a ``messages`` array of ``{messageId, recipients}``. "logged" means the
    mailer accepted the request but is running with sending switched off; both
    count as accepted from our side (the request was taken). Every message id
    is kept so a split send can be cross-referenced.

    Args:
        http_status: Status the mailer answered with; None when it never did.
        response_body: Body text as received; None when it never answered.
        no_response_reason: Exception text when there was no answer.

    Returns:
        The summary, with counts and ids filled in on a readable 200.
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
        status = body.get("status")                                # "sent" or "logged"
        recipients = body.get("recipients")
        messages = body.get("messages") or []
        ids = [m.get("messageId") for m in messages if m.get("messageId")]
        if status in ("sent", "logged"):
            summary.outcome = OUTCOME_ACCEPTED                     # mailer took the request
            summary.succeeded = int(recipients) if recipients is not None else None
            summary.failed = 0
            summary.email_id = ",".join(ids) if ids else None      # every split message id
    except (ValueError, AttributeError, TypeError):
        pass                                                       # keep the raw body; stays unreadable
    return summary


async def send(
    to: list[str], cc: list[str], subject: str, html: str, idempotency_key: str
) -> ExchangeSummary:
    """Send one internal email through the mailer and record the exchange.

    Exactly one ``email_api_log`` row is written, in a ``finally``, holding the
    final answer after any retries. The retry rules: 429 (waiting the capped
    Retry-After), 503, and a 502 whose error is ``hve_token_failed`` are
    retried with the same Idempotency-Key, at most three attempts in all;
    every other error is not retried. An unrecovered 4xx/5xx raises after the
    row is written, like the SMTP2GO path.

    Args:
        to: Internal recipient addresses, blanks already removed.
        cc: Internal CC addresses, blanks already removed.
        subject: Email subject.
        html: Full HTML body.
        idempotency_key: Stable key reused across this call's retries.

    Returns:
        The ``ExchangeSummary`` read off the mailer's final answer.

    Raises:
        httpx.HTTPStatusError: The mailer's last answer was 4xx/5xx.
        httpx.HTTPError: The request never completed.
    """
    payload = build_payload(to, cc, subject, html)                 # body (no secret inside)
    url = send_url()
    headers = {                                                    # key and idempotency go in headers
        "Authorization": f"Bearer {settings.MAILER_KEY}",
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
            if attempt < MAX_ATTEMPTS and _should_retry(http_status, response_body):
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
            sender=settings.MAILER_FROM or "",                     # from is optional for the mailer
            subject=subject,
        )
    if resp is not None and resp.status_code >= 400:               # unrecovered error: log and raise
        logger.error("UCSH mailer %d: %s", resp.status_code, (response_body or "")[:500])
        resp.raise_for_status()
    logger.info(
        "Email sent via UCSH mailer to %s cc %s - subject: %s (%s, id %s)",
        to, cc, subject, summary.outcome, summary.email_id,
    )
    return summary
