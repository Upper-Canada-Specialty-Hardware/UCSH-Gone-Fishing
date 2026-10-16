"""Deliver one queued outbox message, and record what happened (#326).

The decision path only queues a result email or text; this is what actually
sends it, called by the outbox worker. Each row is sent through the same email
and SMS functions the rest of the app uses (so it still writes ``email_api_log``),
then the row is marked ``sent`` with a ``result_sent`` event, or ``failed`` with
an exponential backoff and a ``send_failed`` event. Rehearsal mode drops a row
whose recipient is not on the allowlist, so a dry run never messages a real
person.

Nothing here commits; the worker opens a session per row and commits once, so a
row's new status and its event land together.
"""
import logging
from datetime import timedelta

from app.models.mixins import utcnow
from app.models.outbox import Outbox
from app.models.request_record import RequestRecord
from app.services.request_journal import append_event, log_step

logger = logging.getLogger(__name__)

# Retry backoff: first retry after a minute, doubling, capped at an hour.
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600


def _backoff_seconds(attempts: int) -> int:
    """How long to wait before the next try after ``attempts`` failures.

    Args:
        attempts: How many times this row has now been tried.

    Returns:
        The delay in seconds: 60, 120, 240, ... capped at 3600.
    """
    delay = _BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))     # double each failure
    return min(delay, _BACKOFF_CAP_SECONDS)                         # never wait more than an hour


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
    becomes ``sent`` with a ``result_sent`` event; on failure ``failed`` with a
    backoff and a ``send_failed`` event; in rehearsal mode a disallowed
    recipient becomes ``dropped`` and nothing is sent.

    Args:
        session: The worker's open session for this row (committed by the worker).
        row: The outbox row to deliver.
    """
    if not _allow_recipient(row.recipient):                         # rehearsal: not on the allowlist
        row.status = "dropped"
        row.last_error = "rehearsal mode: recipient not on the allowlist"
        logger.info("Outbox %s dropped in rehearsal mode (%s)", row.id, row.channel)
        return

    row.attempts += 1                                              # count this try, success or not
    request = await session.get(RequestRecord, row.request_id) if row.request_id else None
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
            response = await send_sms(row.recipient, row.body)
            row.provider_message_id = (response or {}).get("sid")   # Twilio's message id
        row.status = "sent"
        row.sent_at = utcnow()
        row.last_error = None
        if request is not None:                                    # tie the send to the request's timeline
            await append_event(
                session, request, "result_sent", "system", None,
                {"channel": row.channel, "to": row.recipient},
                f"{row.idempotency_key}:result_sent",
            )
            log_step(request, "result_sent", channel=row.channel, to=row.recipient)
    except Exception as e:                                         # a provider error: schedule a retry
        row.status = "failed"
        row.last_error = f"{type(e).__name__}: {e}"
        row.next_attempt_at = utcnow() + timedelta(seconds=_backoff_seconds(row.attempts))
        if request is not None:
            await append_event(
                session, request, "send_failed", "system", None,
                {"channel": row.channel, "error": row.last_error},
                f"{row.idempotency_key}:send_failed:{row.attempts}",
            )
            log_step(request, "send_failed", channel=row.channel, attempt=row.attempts)
        logger.warning("Outbox %s failed (attempt %s): %s", row.id, row.attempts, row.last_error)
