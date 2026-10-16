"""The background loops that drive the shadow sync and the SharePoint check (#334, #336).

Two loops run for the life of the app:

- the shadow sync every five minutes, while requests still live in SharePoint,
  keeping the Postgres copy current;
- the post-switch SharePoint check, which only acts once requests are in
  Postgres and is due 30 minutes after the flip, then 1 hour after it, then once
  a day. The schedule counts from the flip time recorded on the ``final_sync``
  SyncRun row, so a restart resumes the same cadence.

Both loops swallow their own errors so a bad run never stops the loop; the next
tick tries again.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.sync_run import SyncRun
from app.repositories.request_store import requests_in_postgres
from app.services.sharepoint_check import run_sharepoint_check
from app.services.shadow_sync import run_shadow_sync

logger = logging.getLogger(__name__)

SHADOW_INTERVAL = 300                   # run the shadow sync every 5 minutes
CHECK_POLL_INTERVAL = 300               # look every 5 minutes whether a check is due
CHECK_AT_30_MIN = timedelta(minutes=30)   # first check: 30 minutes after the flip
CHECK_AT_1_HOUR = timedelta(hours=1)      # second check: 1 hour after the flip
CHECK_DAILY = timedelta(days=1)           # then once a day


def _aware(moment: datetime | None) -> datetime | None:
    """Treat a stored timestamp as UTC so it compares with ``utcnow``.

    SQLite returns naive datetimes even from a timezone column; Postgres returns
    aware ones. This coerces either to aware UTC.

    Args:
        moment: A stored datetime, or None.

    Returns:
        The aware UTC datetime, or None.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def next_check_due(now: datetime, flip_at: datetime, check_count: int, last_check_at: datetime | None) -> bool:
    """Whether the SharePoint check is due now, given the flip and prior checks.

    The cadence is 30 minutes after the flip, then 1 hour after the flip, then
    daily from the last check.

    Args:
        now: The current time (aware UTC).
        flip_at: When the switch happened (the final_sync start).
        check_count: How many checks have run since the flip.
        last_check_at: When the last check ran, or None.

    Returns:
        True when another check should run now.
    """
    flip_at = _aware(flip_at)
    last_check_at = _aware(last_check_at)
    if check_count == 0:
        return now >= flip_at + CHECK_AT_30_MIN
    if check_count == 1:
        return now >= flip_at + CHECK_AT_1_HOUR
    return last_check_at is not None and now >= last_check_at + CHECK_DAILY


async def _check_is_due() -> bool:
    """Read the flip time and prior checks, and decide if a check is due now.

    Returns:
        True when a SharePoint check should run; False before the flip or when
        the next scheduled time has not arrived.
    """
    async with async_session() as session:
        flip_at = (await session.execute(
            select(SyncRun.started_at).where(SyncRun.job == "final_sync").order_by(SyncRun.id.desc()).limit(1)
        )).scalar()
        if flip_at is None:
            return False                                       # no flip yet
        flip_at = _aware(flip_at)
        rows = (await session.execute(
            select(SyncRun.started_at).where(
                SyncRun.job == "sharepoint_check", SyncRun.started_at >= flip_at
            )
        )).scalars().all()
    aware_rows = [_aware(r) for r in rows]
    last = max(aware_rows) if aware_rows else None
    return next_check_due(datetime.now(timezone.utc), flip_at, len(aware_rows), last)


async def _shadow_loop() -> None:
    """Run the shadow sync on its interval, until the app shuts down."""
    while True:
        await asyncio.sleep(SHADOW_INTERVAL)
        try:
            await run_shadow_sync()                            # no-ops once requests are in Postgres
        except Exception:
            logger.exception("Shadow sync loop iteration failed")


async def _check_loop() -> None:
    """Run the SharePoint check whenever it is due, until the app shuts down."""
    while True:
        await asyncio.sleep(CHECK_POLL_INTERVAL)
        try:
            if not await requests_in_postgres():
                continue                                       # the check only runs after the flip
            if await _check_is_due():
                await run_sharepoint_check()
        except Exception:
            logger.exception("SharePoint check loop iteration failed")


def start_shadow_sync_task() -> asyncio.Task:
    """Start the shadow-sync loop.

    Returns:
        The created asyncio task, to cancel on shutdown.
    """
    return asyncio.create_task(_shadow_loop())


def start_sharepoint_check_task() -> asyncio.Task:
    """Start the post-switch SharePoint-check loop.

    Returns:
        The created asyncio task, to cancel on shutdown.
    """
    return asyncio.create_task(_check_loop())
