import asyncio
import logging
import time
from collections import deque

import httpx

from app.config import settings
from app.models.mixins import utcnow
from app.services.email_api_log import (
    OUTCOME_NOT_ATTEMPTED,
    RESPONSE_MAX_CHARS,
    ExchangeSummary,
    classify_response,
    record_exchange,
)

logger = logging.getLogger(__name__)

SMTP2GO_URL = "https://api.smtp2go.com/v3/email/send"
_http = httpx.AsyncClient(timeout=30.0)

# Rate limiter: 10 requests per 60-second sliding window
_MAX_REQUESTS = 10
_WINDOW_SECONDS = 60
_timestamps: deque[float] = deque()

# Recorded on the log row when SMTP2GO is never called.
NO_RECIPIENT_REASON = "No valid recipient address (blank in the Staff Directory?)"


async def send_email_with_dashboard(
    to: list[str],
    subject: str,
    html_body: str,
    primary_employee_id: str | int | None = None,
    **kwargs,
):
    """Send email and automatically append dashboard footer for the primary recipient."""
    footer = ""
    if primary_employee_id and settings.DASHBOARD_FRONTEND_URL:
        try:
            from app.services.dashboard_tokens import build_dashboard_footer_html
            footer = await build_dashboard_footer_html(primary_employee_id)
        except Exception as e:
            logger.debug("Could not build dashboard footer: %s", e)
    await send_email(
        to=to,
        subject=subject,
        html_body=html_body,
        dashboard_footer=footer,
        **kwargs,
    )

    # Only now is it true that this person holds a working link. Recording any
    # earlier would cover people who received nothing: the footer is skipped
    # when the employee lookup fails, swallowed on exception above, and
    # send_email raises on an HTTP or network failure before reaching this
    # line. A 200 whose body rejects the recipient still lands here today;
    # the email_api_log row for that send shows the rejection.
    if footer:
        from app.services.dashboard_link_tracking import record_link_sent
        await record_link_sent(primary_employee_id)


def _build_payload(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None,
    importance: str,
) -> dict:
    """The JSON body posted to SMTP2GO's send endpoint.

    Args:
        to: Recipient addresses, blanks already removed.
        subject: Email subject.
        html_body: Full HTML body, footer included.
        cc: Optional CC addresses; blanks are removed here.
        importance: "High" adds priority headers; "Normal" adds nothing.

    Returns:
        The payload, api_key included. Redacted before it is ever stored.
    """
    payload = {
        "api_key": settings.SMTP2GO_API_KEY,
        "sender": settings.SENDER_EMAIL,
        "to": to,
        "subject": subject,
        "html_body": html_body,
    }
    if cc:
        valid_cc = [addr for addr in cc if addr]
        if valid_cc:
            payload["cc"] = valid_cc
    if importance and importance != "Normal":
        payload["custom_headers"] = [
            {"header": "X-Priority", "value": "1"},
            {"header": "Importance", "value": importance},
        ]
    return payload


async def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    importance: str = "Normal",
    attachments: list[dict] | None = None,
    dashboard_footer: str = "",
) -> ExchangeSummary:
    """Send one email through SMTP2GO and record the exchange in email_api_log.

    Every path out of this function leaves exactly one log row holding the
    redacted request and SMTP2GO's answer verbatim: a send with no usable
    recipient is recorded as not attempted and returns quietly; an HTTP or
    network failure is recorded and then re-raised; an answered call is
    recorded whatever the body says. The row is written in a ``finally`` so a
    raised error cannot skip it, and the writer swallows its own failures so
    logging can never change the outcome of a send.

    Sending behaviour is unchanged from before the log existed: 4xx/5xx and
    network errors raise; a 200 that rejects some or all recipients does not.
    The returned summary is how a caller can tell those cases apart.

    Args:
        to: Recipient addresses; blanks and None are dropped before sending.
        subject: Email subject.
        html_body: HTML body; the dashboard footer is appended when given.
        cc: Optional CC addresses, filtered the same way as ``to``.
        importance: "High" adds priority headers; "Normal" adds nothing.
        attachments: Accepted for signature compatibility; not sent today.
        dashboard_footer: Pre-rendered footer HTML from the dashboard wrapper.

    Returns:
        The ``ExchangeSummary`` read off SMTP2GO's answer (outcome, counts,
        ids, raw body), or a not-attempted summary when nothing was sent.

    Raises:
        httpx.HTTPStatusError: SMTP2GO answered 4xx/5xx (after the row is written).
        httpx.HTTPError: The request never completed (after the row is written).
    """
    full_body = html_body + dashboard_footer if dashboard_footer else html_body
    valid_to = [addr for addr in to if addr]
    payload = _build_payload(valid_to, subject, full_body, cc, importance)

    if not valid_to:
        logger.warning("No valid recipients for email: %s", subject)
        # A blank Staff Directory address raises nothing anywhere else, so this
        # row is often the only evidence that a person was never emailed. The
        # recipients are recorded as the caller passed them (blanks included)
        # so the row shows what the code had to work with.
        summary = ExchangeSummary(
            outcome=OUTCOME_NOT_ATTEMPTED, no_response_reason=NO_RECIPIENT_REASON
        )
        await record_exchange(
            summary,
            request_url=SMTP2GO_URL,
            payload={**payload, "to": list(to)},
            attempted_at=utcnow(),
            duration_ms=None,
        )
        return summary

    await _rate_limit()

    attempted_at = utcnow()                                         # when we asked
    started = time.monotonic()                                      # for the round-trip time
    http_status: int | None = None
    response_body: str | None = None
    no_response_reason: str | None = None
    try:
        resp = await _http.post(SMTP2GO_URL, json=payload)
        http_status = resp.status_code                              # answered, whatever the status
        response_body = resp.text[:RESPONSE_MAX_CHARS]              # the answer, verbatim
    except Exception as e:
        no_response_reason = f"{type(e).__name__}: {e}"             # timeout, DNS, refused, bad TLS
        raise
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = classify_response(http_status, response_body, no_response_reason)
        await record_exchange(                                      # never raises
            summary,
            request_url=SMTP2GO_URL,
            payload=payload,
            attempted_at=attempted_at,
            duration_ms=duration_ms,
        )

    if resp.status_code >= 400:
        logger.error("SMTP2GO %d: %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()                                         # existing behaviour: raise on 4xx/5xx

    if summary.failed:
        # Documented SMTP2GO behaviour: 200 with per-recipient failures in the
        # body. Not raised today (existing behaviour); the log row carries it.
        logger.error(
            "SMTP2GO rejected %s of %s recipient(s) for %r: %s",
            summary.failed, len(valid_to), subject, response_body,
        )
    logger.info(
        "Email sent to %s - subject: %s (SMTP2GO %s, email_id %s)",
        valid_to, subject, summary.outcome, summary.email_id,
    )
    return summary


async def _rate_limit():
    """Sliding window rate limiter — waits if at capacity."""
    now = time.monotonic()
    while _timestamps and _timestamps[0] <= now - _WINDOW_SECONDS:
        _timestamps.popleft()
    if len(_timestamps) >= _MAX_REQUESTS:
        wait = _WINDOW_SECONDS - (now - _timestamps[0])
        logger.info("SMTP2GO rate limit reached, waiting %.1fs", wait)
        await asyncio.sleep(wait)
        return await _rate_limit()
    _timestamps.append(time.monotonic())
