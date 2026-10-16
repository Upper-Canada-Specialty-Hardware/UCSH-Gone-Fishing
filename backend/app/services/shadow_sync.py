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
"""
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.repositories.request_store import requests_in_postgres
from app.services.request_import import request_import_step
from app.services.staff_import import staff_sync_step

logger = logging.getLogger(__name__)

# The steps a sync runs, in order. Each takes (session, sync_run) and returns a
# dict of counts for SyncRun.counts. Staff first (#261), so a person added in
# SharePoint since the last run exists before the requests that name them (#333).
SYNC_STEPS = [staff_sync_step, request_import_step]

# A fixed key for the advisory lock, so every instance contends on the same one.
SYNC_LOCK_KEY = 0x55435348              # "UCSH" as an int; arbitrary but stable

# SQLite / single-process stand-in for the advisory lock.
_single_process_lock = asyncio.Lock()


async def acquire_sync_lock(session: AsyncSession) -> bool:
    """Try to take the sync lock without blocking.

    On Postgres this is a session-scoped advisory lock (held across the commits a
    run makes, released by ``release_sync_lock``). On SQLite it is a single
    in-process lock, which is all a one-process deployment needs.

    Args:
        session: An open session, whose connection holds the lock on Postgres.

    Returns:
        True when the lock was taken, False when another runner holds it.
    """
    if session.bind.dialect.name == "postgresql":
        got = (await session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": SYNC_LOCK_KEY}
        )).scalar()
        return bool(got)
    if _single_process_lock.locked():
        return False                                           # another task is syncing in this process
    await _single_process_lock.acquire()
    return True


async def release_sync_lock(session: AsyncSession) -> None:
    """Release the sync lock taken by ``acquire_sync_lock``.

    Args:
        session: The session that took the lock (same connection on Postgres).
    """
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SYNC_LOCK_KEY})
    elif _single_process_lock.locked():
        _single_process_lock.release()


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
    async with async_session() as session:
        if not await acquire_sync_lock(session):
            logger.info("Shadow sync skipped: another runner holds the lock")
            return {"skipped": "locked"}
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
            await release_sync_lock(session)
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
