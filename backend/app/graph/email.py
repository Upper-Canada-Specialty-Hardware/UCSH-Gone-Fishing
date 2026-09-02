import asyncio
import json
import logging
import time
from collections import deque

import httpx

from app.config import settings
from app.services.email_log import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SENT,
    STATUS_SKIPPED,
    record_email,
)

logger = logging.getLogger(__name__)

SMTP2GO_URL = "https://api.smtp2go.com/v3/email/send"
_http = httpx.AsyncClient(timeout=30.0)

# Rate limiter: 10 requests per 60-second sliding window
_MAX_REQUESTS = 10
_WINDOW_SECONDS = 60
_timestamps: deque[float] = deque()


async def send_email_with_dashboard(
    to: list[str],
    subject: str,
    html_body: str,
    primary_employee_id: str | int | None = None,
    **kwargs,
):
    """Send email and automatically append dashboard footer for the primary recipient.

    The ``primary_employee_id`` is also handed to ``send_email`` so the
    email_log row carries it; that is what lets an admin look the send up by
    Staff Directory id rather than only by address.
    """
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
        primary_employee_id=primary_employee_id,
        **kwargs,
    )

    # Only now is it true that this person holds a working link. Recording any
    # earlier would cover people who received nothing: the footer is skipped
    # when the employee lookup fails, swallowed on exception above, and
    # send_email raises on a delivery failure before reaching this line.
    if footer:
        from app.services.dashboard_link_tracking import record_link_sent
        await record_link_sent(primary_employee_id)


async def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    importance: str = "Normal",
    attachments: list[dict] | None = None,
    dashboard_footer: str = "",
    primary_employee_id: str | int | None = None,
):
    """Send one email through SMTP2GO and record the attempt in ``email_log``.

    Every path out of this function leaves exactly one email_log row: a send
    with no usable recipient is recorded as skipped and returns quietly (the
    existing behaviour); an HTTP or network failure is recorded as failed and
    then re-raised (the existing behaviour); an accepted send is recorded as
    sent, or partial when SMTP2GO rejected some recipients. The row is written
    in a ``finally`` so a raised error cannot skip it, and the writer swallows
    its own failures so the log can never change the outcome of a send.

    Args:
        to: Recipient addresses; blanks and None are dropped before sending.
        subject: Email subject.
        html_body: HTML body; the dashboard footer is appended when given.
        cc: Optional CC addresses, filtered the same way as ``to``.
        importance: "High" adds priority headers; "Normal" adds nothing.
        attachments: Accepted for signature compatibility; not sent today.
        dashboard_footer: Pre-rendered footer HTML from the dashboard wrapper.
        primary_employee_id: Staff Directory id of the main recipient, stored
            on the log row so the send can be found by id.

    Raises:
        httpx.HTTPStatusError: SMTP2GO answered 4xx/5xx (after the row is written).
        httpx.HTTPError: The request never completed (after the row is written).
    """
    full_body = html_body + dashboard_footer if dashboard_footer else html_body
    valid_to = [addr for addr in to if addr]
    if not valid_to:
        logger.warning("No valid recipients for email: %s", subject)
        # A blank Staff Directory address raises nothing anywhere else, so this
        # row is often the only evidence that a person was never emailed.
        await record_email(
            status=STATUS_SKIPPED,
            to=to,
            cc=cc,
            subject=subject,
            primary_employee_id=primary_employee_id,
            error="No valid recipient address (blank in the Staff Directory?)",
        )
        return

    payload = {
        "api_key": settings.SMTP2GO_API_KEY,
        "sender": settings.SENDER_EMAIL,
        "to": valid_to,
        "subject": subject,
        "html_body": full_body,
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

    await _rate_limit()

    # Outcome of this attempt; written to email_log in the finally below
    # whatever happens. Starts as failed so an exception before the status is
    # decided is still recorded truthfully.
    status = STATUS_FAILED
    http_status: int | None = None
    email_id: str | None = None
    request_id: str | None = None
    error: str | None = None
    try:
        resp = await _http.post(SMTP2GO_URL, json=payload)
        http_status = resp.status_code                          # answered, even if 4xx/5xx
        if resp.status_code >= 400:
            logger.error("SMTP2GO %d: %s", resp.status_code, resp.text[:500])
            error = resp.text[:500]                             # keep SMTP2GO's own reason
        resp.raise_for_status()                                 # existing behaviour: raise on 4xx/5xx

        body = resp.json()
        data = body.get("data", {})
        email_id = data.get("email_id")                         # SMTP2GO's id for the accepted message
        request_id = body.get("request_id")
        if data.get("failed", 0) > 0:
            logger.error("SMTP2GO partial failure: %s", data.get("failures"))
            error = json.dumps(data.get("failures"))            # per-recipient rejections
            # Some accepted -> partial; none accepted -> failed (even on HTTP 200).
            status = STATUS_PARTIAL if data.get("succeeded", 0) > 0 else STATUS_FAILED
        else:
            status = STATUS_SENT
    except Exception as e:
        if error is None:
            error = f"{type(e).__name__}: {e}"                  # network error, timeout, bad JSON
        raise
    finally:
        await record_email(
            status=status,
            to=valid_to,
            cc=cc,
            subject=subject,
            primary_employee_id=primary_employee_id,
            smtp2go_email_id=email_id,
            smtp2go_request_id=request_id,
            http_status=http_status,
            error=error,
        )

    logger.info("Email sent to %s — subject: %s", valid_to, subject)


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
