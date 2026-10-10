import asyncio
import hashlib
import logging
import time
import uuid
from collections import deque

import httpx

from app.config import settings
from app.graph import clerk_email, mailer_client
from app.models.mixins import utcnow
from app.services.email_api_log import (
    OUTCOME_NOT_ATTEMPTED,
    RESPONSE_MAX_CHARS,
    ExchangeSummary,
    classify_response,
    combine_summaries,
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


def sender_address() -> str:
    """The From value for SMTP2GO: the display name plus the sending address.

    SMTP2GO accepts ``Name <address>``; the address part must stay the
    verified sender, so only the name is added in front of it.

    Returns:
        ``"<SENDER_NAME> <SENDER_EMAIL>"``, or the bare address when no name is set.
    """
    name = settings.SENDER_NAME.strip()
    if not name:
        return settings.SENDER_EMAIL                          # no name: unchanged from before
    return f"{name} <{settings.SENDER_EMAIL}>"                # inbox shows the app's name


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
    reply_to: str | None = None,
) -> dict:
    """The JSON body posted to SMTP2GO's send endpoint.

    Args:
        to: Recipient addresses, blanks already removed.
        subject: Email subject.
        html_body: Full HTML body, footer included.
        cc: Optional CC addresses; blanks are removed here.
        importance: "High" adds priority headers; "Normal" adds nothing.
        reply_to: Optional Reply-To, sent as a custom header.

    Returns:
        The payload, api_key included. Redacted before it is ever stored.
    """
    payload = {
        "api_key": settings.SMTP2GO_API_KEY,
        "sender": sender_address(),                       # "Name <address>" when a name is set
        "to": to,
        "subject": subject,
        "html_body": html_body,
    }
    if cc:
        valid_cc = [addr for addr in cc if addr]
        if valid_cc:
            payload["cc"] = valid_cc
    headers: list[dict] = []
    if importance and importance != "Normal":
        headers += [
            {"header": "X-Priority", "value": "1"},
            {"header": "Importance", "value": importance},
        ]
    if reply_to:                                                    # answers reach a person, not the sender
        headers.append({"header": "Reply-To", "value": reply_to})
    if headers:                                                     # omit the key when there are none
        payload["custom_headers"] = headers
    return payload


def _domain_of(address: str) -> str:
    """Lower-cased domain part of an address.

    Args:
        address: A recipient address, possibly with surrounding whitespace.

    Returns:
        The part after "@", lower-cased and stripped; "" when there is no "@".
    """
    _, _, domain = address.strip().lower().partition("@")          # "" before/no "@"
    return domain


def _internal_domains() -> set[str]:
    """The domains that route to the UCSH mailer, from settings.

    Returns:
        The comma-separated ``INTERNAL_EMAIL_DOMAINS`` as a lower-cased set,
        blanks dropped.
    """
    return {d.strip().lower() for d in settings.INTERNAL_EMAIL_DOMAINS.split(",") if d.strip()}


def _resolve_reply_to(reply_to: str | None) -> tuple[str | None, str | None]:
    """The Reply-To each service gets for one send.

    The caller's address wins; without one, ``EMAIL_REPLY_TO`` is used. The
    mailer refuses an outside Reply-To, so it only gets an internal one;
    SMTP2GO takes any address.

    Args:
        reply_to: The caller's Reply-To, or None.

    Returns:
        ``(for_smtp2go, for_mailer)``; either may be None (no Reply-To).
    """
    address = (reply_to or settings.EMAIL_REPLY_TO or "").strip()  # caller first, then the default
    if not address:                                                # nothing to set
        return None, None
    internal = _domain_of(address) in _internal_domains()          # mailer rule: internal only
    return address, (address if internal else None)


def _mailer_usable() -> bool:
    """Whether internal recipients should go to the UCSH mailer right now.

    Returns:
        True only when the mailer is switched on and configured. Switched on
        but missing its url or key is a misconfiguration: it is logged as an
        error and treated as unusable, so those recipients fall back to
        SMTP2GO rather than being dropped.
    """
    if not settings.MAILER_ENABLED:                                # off: use SMTP2GO
        return False
    if not mailer_client.is_configured():                          # on but not set up
        logger.error(
            "MAILER_ENABLED is on but MAILER_URL/MAILER_KEY are not set; "
            "UCSH recipients fall back to SMTP2GO"
        )
        return False
    return True


def _clerk_usable() -> bool:
    """Whether external recipients should go to Clerk right now.

    Returns:
        True only when Clerk is switched on and configured. Switched on but
        missing its key or sender is logged as an error and treated as
        unusable, so those recipients fall back to SMTP2GO.
    """
    if not settings.CLERK_EMAIL_ENABLED:                           # off: use SMTP2GO
        return False
    if not clerk_email.is_configured():                            # on but not set up
        logger.error(
            "CLERK_EMAIL_ENABLED is on but CLERK_SECRET_KEY/CLERK_FROM_EMAIL are not set; "
            "external recipients fall back to SMTP2GO"
        )
        return False
    return True


def _route_recipients(
    valid_to: list[str], valid_cc: list[str]
) -> tuple[dict, dict, list[tuple[str, str]]]:
    """Split recipients into per-service groups by domain.

    Each address goes to the service its domain designates: internal (UCSH)
    addresses to the UCSH mailer, every other address to Clerk. When the
    designated service is off or misconfigured, that address falls back to
    SMTP2GO. Clerk never receives an internal address and the mailer never
    receives an external one.

    Args:
        valid_to: To addresses with blanks already removed.
        valid_cc: CC addresses with blanks already removed.

    Returns:
        ``(smtp2go, mailer, clerk)`` where ``smtp2go`` and ``mailer`` are
        ``{"to": [...], "cc": [...]}`` and ``clerk`` is a list of
        ``(address, field)`` because Clerk sends one recipient per call.
    """
    internal = _internal_domains()
    mailer_usable = _mailer_usable()
    clerk_usable = _clerk_usable()
    smtp2go: dict = {"to": [], "cc": []}
    mailer: dict = {"to": [], "cc": []}
    clerk: list[tuple[str, str]] = []
    for field, addresses in (("to", valid_to), ("cc", valid_cc)):
        for addr in addresses:
            if _domain_of(addr) in internal:                       # a staff address
                (mailer if mailer_usable else smtp2go)[field].append(addr)
            else:                                                  # an outside address
                if clerk_usable:
                    clerk.append((addr, field))
                else:
                    smtp2go[field].append(addr)
    return smtp2go, mailer, clerk


def _clerk_key(base_key: str, address: str) -> str:
    """Per-recipient Clerk idempotency key derived from the send's base key.

    Clerk allows only letters, digits, underscore and hyphen, so the address
    is folded in as a hex digest rather than appended raw.

    Args:
        base_key: The send-wide base key (hex).
        address: The recipient the key is for.

    Returns:
        ``<base>_<16 hex of the address>``, well under Clerk's 255-char limit.
    """
    suffix = hashlib.sha256(address.strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"{base_key}_{suffix}"


async def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    importance: str = "Normal",
    attachments: list[dict] | None = None,
    dashboard_footer: str = "",
    reply_to: str | None = None,
) -> ExchangeSummary:
    """Send one email, routing each recipient to the right service, and log it.

    The signature and the contract for callers are unchanged. What is new is
    that recipients are split by domain across up to three services: UCSH
    (internal) addresses go to the UCSH mailer, every other address to Clerk,
    and anything whose service is switched off (or misconfigured) stays on
    SMTP2GO. With both new services off (the default), every recipient routes
    to SMTP2GO and the behaviour and logging are exactly what they were.

    Every HTTP call made for the send leaves exactly one ``email_api_log`` row
    (written in a ``finally``; the writer never raises), so a Clerk fan-out of
    three recipients leaves three rows and a mailer call leaves one. Each
    service classifies its own answer into the shared summary shape, so the
    admin Email Log tab reads them all the same way.

    Error contract (unchanged for callers): if any group fails with an HTTP or
    network error, the other groups are still attempted, then the first failure
    is raised. A 200 that merely rejects recipients does not raise, as before.

    Args:
        to: Recipient addresses; blanks and None are dropped before sending.
        subject: Email subject.
        html_body: HTML body; the dashboard footer is appended when given.
        cc: Optional CC addresses, filtered the same way as ``to``.
        importance: "High" adds priority headers; "Normal" adds nothing
            (SMTP2GO only; the other services do not take an importance flag).
        attachments: Accepted for signature compatibility; not sent today.
        dashboard_footer: Pre-rendered footer HTML from the dashboard wrapper.
        reply_to: The person a reply should reach (the manager on an
            employee's email, the employee on a manager's). Falls back to
            ``EMAIL_REPLY_TO``; see ``_resolve_reply_to``.

    Returns:
        A single ``ExchangeSummary`` covering the whole send (see
        ``combine_summaries``): the one call's summary when only one service
        was used, or the folded summary across services otherwise.

    Raises:
        httpx.HTTPStatusError: A service answered 4xx/5xx (rows written first).
        httpx.HTTPError: A request never completed (rows written first).
    """
    # Content plus any dashboard footer, then the one shared layout, applied
    # here so every email (templated or inline) is wrapped exactly once and no
    # caller has to know about it. render_layout is idempotent on an already
    # wrapped body. Imported locally to avoid an import cycle at module load.
    from app.templates_render import render_layout
    full_body = html_body + dashboard_footer if dashboard_footer else html_body
    full_body = render_layout(full_body)                           # UCSH Out of Office shell
    valid_to = [addr for addr in to if addr]                       # drop blanks/None
    valid_cc = [addr for addr in (cc or []) if addr]

    smtp2go, mailer, clerk = _route_recipients(valid_to, valid_cc)
    # A group can end up with CC addresses but no To (the To went to another
    # service). SMTP2GO treats "no To" as nothing to send, so promote the CC
    # to To for that group; they still get the mail.
    for group in (smtp2go, mailer):
        if not group["to"] and group["cc"]:
            group["to"], group["cc"] = group["cc"], []
    base_key = uuid.uuid4().hex                                    # reused across a call's retries
    reply_smtp2go, reply_mailer = _resolve_reply_to(reply_to)       # per-service Reply-To

    summaries: list[ExchangeSummary] = []
    first_error: Exception | None = None

    # SMTP2GO: send its group when it has recipients, or when nothing is
    # routable anywhere (which reproduces the historical not-attempted row on
    # the SMTP2GO url, so a wholly-blank send behaves exactly as it always has).
    nothing_routable = not (
        smtp2go["to"] or smtp2go["cc"] or mailer["to"] or mailer["cc"] or clerk
    )
    if smtp2go["to"] or smtp2go["cc"] or nothing_routable:
        try:
            summaries.append(await _send_via_smtp2go(
                smtp2go["to"], smtp2go["cc"], subject, full_body, importance, raw_to=to,
                reply_to=reply_smtp2go,
            ))
        except Exception as e:                                     # keep going; raise it at the end
            first_error = first_error or e

    # UCSH mailer: one HTTP call for the whole internal group.
    if mailer["to"] or mailer["cc"]:
        try:
            summaries.append(await mailer_client.send(
                mailer["to"], mailer["cc"], subject, full_body, base_key, reply_to=reply_mailer
            ))
        except Exception as e:
            refused = mailer_client.refused_addresses(e)             # set only for recipient_not_internal
            if not refused:
                first_error = first_error or e
            else:
                # The mailer sent nothing: some addresses are outside its allowed domains
                # (INTERNAL_EMAIL_DOMAINS lists more than the mailer accepts). Re-send the
                # rest through the mailer and route the refused ones like outside addresses.
                logger.warning(
                    "UCSH mailer refused %s as not internal; sending them the outside way. "
                    "Check INTERNAL_EMAIL_DOMAINS against the mailer's allowed domains.", sorted(refused),
                )
                kept = {f: [a for a in mailer[f] if a.strip().lower() not in refused] for f in ("to", "cc")}
                moved = [(a, f) for f in ("to", "cc") for a in mailer[f] if a.strip().lower() in refused]
                if not kept["to"] and kept["cc"]:                    # no To left: promote the CC
                    kept["to"], kept["cc"] = kept["cc"], []
                if kept["to"]:
                    try:
                        summaries.append(await mailer_client.send(   # a new body needs a new key
                            kept["to"], kept["cc"], subject, full_body, f"{base_key}-kept",
                            reply_to=reply_mailer,
                        ))
                    except Exception as e2:
                        first_error = first_error or e2
                if _clerk_usable():
                    clerk.extend(moved)                              # sent with the Clerk group below
                else:
                    fallback = {f: [a for a, g in moved if g == f] for f in ("to", "cc")}
                    if not fallback["to"]:                           # no To: promote the CC
                        fallback["to"], fallback["cc"] = fallback["cc"], []
                    try:
                        summaries.append(await _send_via_smtp2go(
                            fallback["to"], fallback["cc"], subject, full_body, importance, raw_to=fallback["to"],
                            reply_to=reply_smtp2go,
                        ))
                    except Exception as e3:
                        first_error = first_error or e3

    # Clerk: one HTTP call per external recipient, each with its own key.
    for address, field in clerk:
        try:
            summaries.append(await clerk_email.send(
                address, field, subject, full_body, _clerk_key(base_key, address)
            ))
        except Exception as e:
            first_error = first_error or e

    if first_error is not None:                                    # callers rely on this raising
        raise first_error
    return combine_summaries(summaries)


async def _send_via_smtp2go(
    to: list[str],
    cc: list[str],
    subject: str,
    full_body: str,
    importance: str,
    *,
    raw_to: list[str],
    reply_to: str | None = None,
) -> ExchangeSummary:
    """Send one email through SMTP2GO and record the exchange. Fallback mailer.

    This is the original single-provider send, unchanged in behaviour: a send
    with no usable recipient is recorded as not attempted and returns quietly;
    an HTTP or network failure is recorded in a ``finally`` and then re-raised;
    a 200 that rejects some or all recipients is recorded and does not raise.
    The SMTP2GO rate limiter applies only here.

    Args:
        to: To addresses, blanks already removed.
        cc: CC addresses, blanks already removed.
        subject: Email subject.
        full_body: HTML body with the dashboard footer already appended.
        importance: "High" adds priority headers; "Normal" adds nothing.
        raw_to: The caller's original ``to`` (blanks included), recorded on the
            not-attempted row so it shows what the code had to work with.
        reply_to: Optional Reply-To header.

    Returns:
        The ``ExchangeSummary`` read off SMTP2GO's answer, or a not-attempted
        summary when there was no usable recipient.

    Raises:
        httpx.HTTPStatusError: SMTP2GO answered 4xx/5xx (after the row is written).
        httpx.HTTPError: The request never completed (after the row is written).
    """
    payload = _build_payload(to, subject, full_body, cc, importance, reply_to)

    if not to:
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
            payload={**payload, "to": list(raw_to)},
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
            summary.failed, len(to), subject, response_body,
        )
    logger.info(
        "Email sent to %s - subject: %s (SMTP2GO %s, email_id %s)",
        to, subject, summary.outcome, summary.email_id,
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
