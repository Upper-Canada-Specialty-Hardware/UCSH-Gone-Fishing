"""Deliver one queued outbox message, and record what happened (#326).

The decision path only queues a result email or text; this is what actually
sends it, called by the outbox worker. Each row is sent through the same email
and SMS functions the rest of the app uses (so it still writes ``email_api_log``),
then the row is marked ``sent`` with a ``result_sent`` event, or ``failed`` and
retried with an exponential backoff. A 4xx (other than 429) or a bad phone
number fails the row at once; only the final failure records a ``send_failed``
event, so a flaky provider leaves one timeline line, not ten (#419, #424).
Rehearsal mode drops a row whose recipient is not on the allowlist, and a
manager ask for a request already decided or edited is dropped unsent (#418),
so a dry run never messages a real person and a stale link never goes out.

Nothing here commits; the worker opens a session per row and commits once, so a
row's new status and its event land together.
"""
import logging
from datetime import timedelta

import httpx

from app.models.mixins import utcnow
from app.models.outbox import Outbox
from app.models.request_record import RequestRecord
from app.services.request_journal import append_event, log_step
from app.services.sms import InvalidPhoneNumber, mask_phone, scrub_error_text

logger = logging.getLogger(__name__)

# Message types that ask a manager to decide: queued against one approval
# version, so they must not go out once the request is decided or its version
# has moved on (an admin edit) (#418).
_MANAGER_ASK_TYPES = ("manager_approval", "reminder")

# Retry backoff: first retry after a minute, doubling, capped at an hour.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600

# A row that keeps failing is abandoned after this many tries, so a permanently
# bad recipient or provider is not retried forever. The worker's claim query
# skips a failed row at this cap (see tasks/outbox_worker.py).
MAX_OUTBOX_ATTEMPTS = 10

# A delivered message's type -> the timeline event a successful send records, so
# the stage reads right: a queued manager ask becomes "Waiting on manager", the
# employee receipt "Submitted, receipt sent", a reminder "Reminder sent", and a
# decision's result or rejection "Result sent". Anything else records result_sent.
# A bereavement_alert maps to None on purpose: it is an HR side-notice about a
# still-pending request, so a successful send must NOT advance the request's
# stage (writing result_sent here made the status tracker finish early) (finding 3).
_SENT_EVENT_FOR_TYPE = {
    "manager_approval": "sent_to_manager",
    "receipt": "receipt_sent",
    "reminder": "reminder_sent",
    "result": "result_sent",
    "rejection": "result_sent",
    "bereavement_alert": None,
    # A cancel notice (#519) follows the "cancelled" event already on the
    # timeline; delivering it must not move the stage back to "Result sent".
    "cancel_notice": None,
}


def _sent_event(message_type: str) -> str | None:
    """The event type a successful send of this message records.

    Args:
        message_type: The outbox row's message_type.

    Returns:
        The request_events type to append; None for a send that records no
        stage-changing event (the HR bereavement alert), and ``result_sent`` for
        anything unmapped.
    """
    return _SENT_EVENT_FOR_TYPE.get(message_type, "result_sent")


def _backoff_seconds(attempts: int) -> int:
    """How long to wait before the next try after ``attempts`` failures.

    Args:
        attempts: How many times this row has now been tried.

    Returns:
        The delay in seconds: 60, 120, 240, ... capped at 3600.
    """
    delay = _BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))     # double each failure
    return min(delay, _BACKOFF_CAP_SECONDS)                         # never wait more than an hour


def _queued_version(idempotency_key: str) -> int | None:
    """The approval version a manager-ask row was queued for, read from its key.

    A step key is ``{kind}:{item_id}:{step}:v{version}[:...]``, so the version is
    the first ``v<number>`` token; kind, item_id and the recipient never take
    that shape, so the match is unambiguous (#418).

    Args:
        idempotency_key: The outbox row's idempotency key.

    Returns:
        The version, or None when the key carries none.
    """
    for token in (idempotency_key or "").split(":"):
        if token.startswith("v") and token[1:].isdigit():          # the v-prefixed version token
            return int(token[1:])
    return None


# The only HTTP statuses that mean the RECIPIENT or the request content is wrong,
# so retrying is pointless: a bad request (400, which is also how Twilio returns
# its 21211/21614/21617 bad-number codes), a missing resource (404) and an
# unprocessable entity (422). Everything else 4xx is account-level and fixable:
# 401/403 (bad credentials or a key change), 402 (Twilio out of balance, the
# July/Aug outage), 429 (rate limiting) all keep the backoff so the row sends
# once the account is fixed (finding 7).
_PERMANENT_STATUS_CODES = (400, 404, 422)


def _is_permanent_failure(error: Exception) -> bool:
    """Whether a send error should fail the row at once, with no retry (#419).

    Args:
        error: The exception the send raised.

    Returns:
        True only for a recipient-level error: a bad number
        (``InvalidPhoneNumber``) or an HTTP 400 / 404 / 422. False for every
        account-level or transient error (401, 402, 403, 429, 5xx and network),
        which keeps the backoff so the row retries once the account is fixed.
    """
    if isinstance(error, InvalidPhoneNumber):
        return True                                                # Twilio will always refuse this number
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _PERMANENT_STATUS_CODES   # the recipient/content is wrong
    return False


def _error_text(error: Exception) -> str:
    """A short error string that keeps the service's own code and message (#419).

    Args:
        error: The exception the send raised.

    Returns:
        ``HTTP {status}: {code} {message}`` for an HTTP error (Twilio's JSON
        ``code``/``message``, or the mail API's error body), or ``Type: message``
        for anything else.
    """
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        body = ""
        try:
            data = error.response.json()                           # Twilio and the mail API answer JSON
            if isinstance(data, dict) and ("code" in data or "message" in data):
                code = data.get("code")                            # Twilio's numeric error code
                message = data.get("message") or data.get("error") or ""
                body = f"{code} {message}".strip()
            else:
                body = str(data)[:200]                             # some other JSON body: keep a little
        except Exception:                                          # noqa: BLE001 - a non-JSON body
            body = (error.response.text or "")[:200]
        return f"HTTP {status}: {body}".strip()
    return f"{type(error).__name__}: {error}"


def _journal_recipient(channel: str, recipient: str) -> str:
    """The recipient to put in a timeline event, masking a phone number (#420).

    Args:
        channel: "email" or "sms".
        recipient: The real recipient the row was sent to.

    Returns:
        The address for email, or the last-four-only mask for an SMS number.
    """
    return mask_phone(recipient) if channel == "sms" else recipient


def _allow_recipient(recipient: str) -> bool:
    """Whether this recipient may be messaged now (rehearsal gate).

    The rehearsal module belongs to the sync work; until that is joined it may
    not exist, and then every recipient is allowed (normal running).

    Args:
        recipient: The address or phone number.

    Returns:
        True to send, False to drop in rehearsal mode.
    """
    try:
        from app.services.rehearsal import allow_recipient
    except ImportError:
        return True                                                # no rehearsal module: send normally
    return allow_recipient(recipient)


async def send_outbox_row(session, row: Outbox) -> None:
    """Send one claimed outbox row and record the outcome (no commit).

    The row must already be marked ``sending`` by the worker. On success it
    becomes ``sent`` with a matching timeline event; on a transient failure it
    becomes ``failed`` with a backoff (no event yet); on a permanent failure or
    the attempt cap it becomes ``failed`` with no further retry and one
    ``send_failed`` event (#419, #424). A disallowed rehearsal recipient, or a
    manager ask whose request is already decided or edited, becomes ``dropped``
    and nothing is sent (#418).

    Args:
        session: The worker's open session for this row (committed by the worker).
        row: The outbox row to deliver.
    """
    if not _allow_recipient(row.recipient):                         # rehearsal: not on the allowlist
        row.status = "dropped"
        row.last_error = "rehearsal mode: recipient not on the allowlist"
        logger.info("Outbox %s dropped in rehearsal mode (%s)", row.id, row.channel)
        return

    request = await session.get(RequestRecord, row.request_id) if row.request_id else None

    # A manager ask or reminder must not go out once its request is decided, or
    # once an admin edit has bumped the approval version past the one this row
    # was queued for: the links would be stale and the decision already made.
    # Drop it (the worker never re-claims a dropped row), counting no attempt (#418).
    if request is not None and row.message_type in _MANAGER_ASK_TYPES:
        queued_version = _queued_version(row.idempotency_key)      # the version these links carry
        if request.status != "pending" or (
            queued_version is not None and request.approval_version != queued_version
        ):
            row.status = "dropped"
            row.last_error = "skipped: request no longer pending or its approval version changed"
            logger.info(
                "Outbox %s skipped (%s): %s #%s is %s at v%s (queued for v%s)",
                row.id, row.channel, request.kind, request.item_id,
                request.status, request.approval_version, queued_version,
            )
            return

    row.attempts += 1                                              # count this try, success or not
    try:
        if row.channel == "email":
            # send_email applies the shared layout and writes email_api_log itself.
            from app.graph.email import send_email
            summary = await send_email([row.recipient], row.subject or "", row.body)
            row.provider_message_id = getattr(summary, "email_id", None)   # the provider's id, when it gave one
            # The email_api_log row id is not handed back by the send API, so it
            # is left unset here; the log row still exists, found by recipient/time.
        else:
            from app.services.sms import send_sms
            # Label the text-log row with what this outbox row is: its message
            # type as the kind, and the request it belongs to, so the admin Text
            # Log can group every text for one request (#494).
            response = await send_sms(
                row.recipient, row.body,
                kind=row.message_type,
                request_kind=request.kind if request is not None else None,
                request_item_id=request.item_id if request is not None else None,
                outbox_id=row.id,
                request_id=row.request_id,
            )
            row.provider_message_id = (response or {}).get("sid")   # Twilio's message id
        row.status = "sent"
        row.sent_at = utcnow()
        row.last_error = None
        event_type = _sent_event(row.message_type)                # the event that matches this message
        # event_type is None for an HR side-notice (bereavement alert): it records
        # no timeline event, so a delivered alert never advances a pending
        # request's stage (finding 3).
        if request is not None and event_type is not None:        # tie the send to the request's timeline
            # A manager-approval send names the channel and manager; every other
            # send records who it reached, so the timeline reads from the detail.
            if event_type == "sent_to_manager":
                detail = {"to": row.channel, "manager": request.manager_name}
            else:
                # An SMS recipient is masked to its last four digits (#420); an
                # email address is shown in full.
                detail = {"channel": row.channel, "to": _journal_recipient(row.channel, row.recipient)}
            await append_event(
                session, request, event_type, "system", None, detail,
                f"{row.idempotency_key}:{event_type}",
            )
            log_step(request, event_type, channel=row.channel,
                     to=_journal_recipient(row.channel, row.recipient))
    except Exception as e:                                         # a provider error: retry or give up
        row.status = "failed"
        # Keep the service's own code and message, with the Twilio Account SID and
        # any phone number scrubbed out of the text before it is stored, logged or
        # written to a timeline event (the error carries the API URL and quotes the
        # recipient number) (#419, #420, finding 5).
        row.last_error = scrub_error_text(_error_text(e))
        # A bad recipient or a 4xx (not 429) is permanent: fail at once rather
        # than retry ten times. 429, 5xx and network errors keep the backoff.
        give_up = _is_permanent_failure(e) or row.attempts >= MAX_OUTBOX_ATTEMPTS
        if give_up:
            # No further retry: a failed row with no next_attempt_at is terminal,
            # and the worker's claim query leaves it alone (tasks/outbox_worker.py).
            row.next_attempt_at = None
            logger.error(
                "Outbox %s gave up after %s attempt(s) (%s): %s",
                row.id, row.attempts, row.channel, row.last_error,
            )
            # Only the FINAL failure records a timeline row; the retries in
            # between add nothing, so a flaky provider leaves one line, not ten (#424).
            if request is not None:
                await append_event(
                    session, request, "send_failed", "system", None,
                    {"channel": row.channel, "error": row.last_error},
                    f"{row.idempotency_key}:send_failed",
                )
                log_step(request, "send_failed", channel=row.channel, attempts=row.attempts)
        else:                                                      # still retrying: back off and try later
            row.next_attempt_at = utcnow() + timedelta(seconds=_backoff_seconds(row.attempts))
            logger.warning("Outbox %s failed (attempt %s, will retry): %s", row.id, row.attempts, row.last_error)
