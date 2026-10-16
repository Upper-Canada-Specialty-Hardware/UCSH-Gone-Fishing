"""The background worker that drains the outbox and sends what is due (#326).

A decision only queues its result email and text; this worker delivers them, so
approving never waits on the email provider. It claims due rows (``pending``, or
``failed`` whose backoff has elapsed), marks each ``sending``, and hands it to
``send_outbox_row``. On Postgres it claims with ``FOR UPDATE SKIP LOCKED`` so
several instances never grab the same row; on SQLite one process claims plainly.

It wakes two ways: an in-process event the decision path sets right after it
commits, so mail goes out within a second, and a poll every 15 seconds as a
backstop (and to retry failed rows once their backoff passes).
"""
import asyncio
import logging

from sqlalchemy import or_, select

from app.database import async_session
from app.models.mixins import utcnow
from app.models.outbox import Outbox
from app.services.outbox_sender import send_outbox_row

logger = logging.getLogger(__name__)

POLL_SECONDS = 15                 # backstop poll, and the retry check for failed rows
CLAIM_BATCH = 20                  # how many rows to claim per pass

# Set by the decision path after it commits, so a new message goes out at once.
_wakeup = asyncio.Event()


def kick_outbox() -> None:
    """Wake the worker now (called right after a decision commits).

    Safe to call when the worker is not running (tests): it only sets an event.
    """
    try:
        _wakeup.set()
    except RuntimeError:                                           # no running loop (never in practice): ignore
        pass


async def _claim_due(limit: int) -> list[int]:
    """Claim up to ``limit`` due rows, mark them sending, and return their ids.

    Args:
        limit: The most rows to claim in one pass.

    Returns:
        The ids of the rows this call claimed.
    """
    now = utcnow()
    async with async_session() as session:
        statement = (
            select(Outbox)
            .where(
                Outbox.status.in_(("pending", "failed")),          # never a sent/sending/dropped row
                or_(Outbox.next_attempt_at.is_(None), Outbox.next_attempt_at <= now),  # due now
            )
            .order_by(Outbox.id)
            .limit(limit)
        )
        if session.bind.dialect.name == "postgresql":              # several instances: do not grab the same row
            statement = statement.with_for_update(skip_locked=True)
        rows = (await session.execute(statement)).scalars().all()
        ids = [row.id for row in rows]
        for row in rows:
            row.status = "sending"                                 # claimed: other workers skip it
        await session.commit()
    return ids


async def _drain() -> None:
    """Send every due row, a batch at a time, until none are left."""
    while True:
        ids = await _claim_due(CLAIM_BATCH)
        if not ids:
            return
        for row_id in ids:
            async with async_session() as session:                 # one row, one transaction
                row = await session.get(Outbox, row_id)
                if row is None or row.status != "sending":         # vanished or re-claimed: skip
                    continue
                await send_outbox_row(session, row)
                await session.commit()


async def _loop() -> None:
    """Drain on every wake, then wait for the next kick or the poll timeout."""
    while True:
        try:
            await _drain()
        except Exception:                                          # one bad pass must not kill the worker
            logger.exception("Outbox drain failed")
        try:
            await asyncio.wait_for(_wakeup.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass                                                   # the backstop poll fired
        _wakeup.clear()


def start_outbox_worker() -> asyncio.Task:
    """Start the outbox worker as a background task.

    Returns:
        The task, so the lifespan can cancel it on shutdown.
    """
    logger.info("Outbox worker started")
    return asyncio.create_task(_loop())
