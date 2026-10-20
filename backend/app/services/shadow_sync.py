"""Keep Postgres in step with SharePoint while SharePoint is still the source (#334).

Before the switch, requests are read and written in SharePoint, and Postgres is
a shadow copy kept current so the flip is instant and safe. Every five minutes
this runs each registered step, which reads SharePoint and upserts Postgres;
nothing is ever written back to SharePoint. The same steps run one last time,
inside the flip, as the final sync.

Only one runner may sync at a time across all app instances, or two would race
on the same rows. A Postgres advisory lock enforces that in production; on
SQLite (local and tests) a single in-process lock stands in, which is all one
process needs. The flip takes the same lock, so a sync and the flip never
overlap. These lock helpers are shared with ``storage_switch``.

The advisory lock is held on its OWN connection for the whole run, not on the
session that does the work (#334 review, finding 3). ``pg_try_advisory_lock`` is
tied to the connection it ran on; the work session commits several times during
a run, and each commit hands its pooled connection back, so a later unlock on a
different pooled connection would silently fail and leave the lock stuck. A
dedicated connection, taken in ``acquire_sync_lock`` and closed in
``release_sync_lock``, releases the exact lock it took.
"""
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.database import async_session, engine
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.repositories.request_store import requests_in_postgres
from app.services.balance_history_import import history_sync_step
from app.services.holiday_import import holiday_sync_step
from app.services.request_import import request_import_step
from app.services.staff_import import staff_sync_step

logger = logging.getLogger(__name__)

# The steps a sync runs, in order. Each takes (session, sync_run) and returns a
# dict of counts for SyncRun.counts. Staff first (#261), so a person added in
# SharePoint since the last run exists before the requests that name them (#333).
# The SharePoint history step (#357) comes right after the requests it reads.
# Holidays last (#335); they stand alone, so order against the others is moot.
SYNC_STEPS = [staff_sync_step, request_import_step, history_sync_step, holiday_sync_step]

# A fixed key for the advisory lock, so every instance contends on the same one.
SYNC_LOCK_KEY = 0x55435348              # "UCSH" as an int; arbitrary but stable

# SQLite / single-process stand-in for the advisory lock.
_single_process_lock = asyncio.Lock()


class SyncLock:
    """The one-runner sync lock, held for a whole run independent of the work session.

    On Postgres it owns a dedicated connection and takes a session-level advisory
    lock on it; the lock lives until this same connection releases and closes it,
    so the work session's commits (which recycle pooled connections) never touch
    it. On SQLite it holds the in-process ``asyncio.Lock``, which is all a
    one-process deployment needs.

    Attributes:
        _conn: The dedicated Postgres connection holding the lock, or None.
    """

    def __init__(self):
        """Start with no connection held; ``acquire`` takes one on Postgres."""
        self._conn: AsyncConnection | None = None               # set only on Postgres, only while held

    async def acquire(self) -> bool:
        """Try to take the lock without blocking.

        Returns:
            True when the lock was taken, False when another runner holds it.
        """
        if engine.dialect.name == "postgresql":
            conn = await engine.connect()                       # a connection this lock alone holds
            got = (await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": SYNC_LOCK_KEY}
            )).scalar()
            if not got:
                await conn.close()                              # someone else holds it: drop the connection
                return False
            self._conn = conn                                   # keep it open so the lock stays held
            return True
        if _single_process_lock.locked():
            return False                                        # another task is syncing in this process
        await _single_process_lock.acquire()
        return True

    async def release(self) -> None:
        """Release the lock on the same connection that took it, then close it.

        Safe to call once per acquire; closing the connection would drop a
        session-level advisory lock anyway, but the explicit unlock returns it to
        the pool clean.
        """
        if engine.dialect.name == "postgresql":
            if self._conn is not None:                          # only when Postgres took one
                try:
                    await self._conn.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_LOCK_KEY}
                    )
                finally:
                    await self._conn.close()                    # end the dedicated connection
                    self._conn = None
        elif _single_process_lock.locked():
            _single_process_lock.release()


async def acquire_sync_lock() -> SyncLock | None:
    """Take the sync lock, or return None when another runner holds it.

    Returns:
        A held ``SyncLock`` to pass to ``release_sync_lock`` in a ``finally``, or
        None when the lock could not be taken.
    """
    lock = SyncLock()
    return lock if await lock.acquire() else None


async def release_sync_lock(lock: SyncLock | None) -> None:
    """Release a lock taken by ``acquire_sync_lock``. A no-op for None.

    Args:
        lock: The held lock, or None when it was never taken.
    """
    if lock is not None:
        await lock.release()


async def run_shadow_sync() -> dict:
    """Run every sync step once, as the single allowed runner. Reads SharePoint only.

    Does nothing once requests already live in Postgres (the shadow phase is
    over) or when another runner holds the lock. Writes one ``shadow_sync``
    SyncRun row with the per-step counts.

    Returns:
        ``{"skipped": "<why>"}`` when it did not run, else the per-step counts.
    """
    if await requests_in_postgres():
        return {"skipped": "requests already in postgres"}      # the shadow phase is done
    lock = await acquire_sync_lock()                            # on its own connection, before the work session
    if lock is None:
        logger.info("Shadow sync skipped: another runner holds the lock")
        return {"skipped": "locked"}
    try:
        async with async_session() as session:
            run = SyncRun(job="shadow_sync")
            session.add(run)
            await session.flush()
            try:
                counts = await _run_steps(session, run)
                run.counts = counts
                run.finished_at = utcnow()
                await session.commit()
            except Exception as e:
                run.error = str(e)
                run.finished_at = utcnow()
                await session.commit()
                logger.exception("Shadow sync failed")
                counts = {"error": str(e)}
    finally:
        await release_sync_lock(lock)                           # release on the lock's own connection
    return counts


async def _run_steps(session: AsyncSession, run: SyncRun) -> dict:
    """Run every registered sync step within one session.

    Args:
        session: The run's open session; the caller commits.
        run: The SyncRun row the steps belong to.

    Returns:
        ``{step name: that step's counts}``.
    """
    counts: dict = {}
    for step in SYNC_STEPS:
        counts[step.__name__] = await step(session, run)        # each step upserts and returns counts
    return counts
