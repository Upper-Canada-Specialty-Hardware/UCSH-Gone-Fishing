"""Flip requests (and staff, when ready) from SharePoint to Postgres (#335).

The switch is one deliberate step: refuse that it has not already happened, take
the sync lock so no shadow sync is running, pause the request and staff writes
for its short duration, run every sync step one last time so Postgres holds
SharePoint's latest state, record that final sync, then set the
``requests_storage`` app setting to ``postgres`` (and ``staff_storage`` too).
From then on the app reads and writes requests in Postgres, and the post-switch
SharePoint check (``sharepoint_check``) watches the old lists for anything that
still lands there. The pause is cleared in ``finally``, so writes resume even if
the flip raises, and it carries its own expiry so a crash never leaves the app
paused (see ``switch_pause``).

``flip_back`` reverses the settings for a rehearsal, so a rehearsal can flip,
look around, and flip back without leaving the app pointed at Postgres.
"""
import asyncio
import logging

from app.database import async_session
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.repositories.request_store import requests_in_postgres
from app.services import app_settings
from app.services.app_settings import POSTGRES, REQUESTS_STORAGE, SHAREPOINT, set_setting
from app.services.shadow_sync import _run_steps, acquire_sync_lock, release_sync_lock
from app.services.switch_pause import begin_pause, end_pause

logger = logging.getLogger(__name__)

# How long the switch waits after pausing before the final sync reads SharePoint,
# so an approval or edit that started just before the pause finishes its
# SharePoint writes first and the final sync sees them.
PAUSE_SETTLE_SECONDS = 15

STAFF_STORAGE = "staff_storage"         # the staff store builder adds this setting; flip it too when present


class AlreadySwitched(RuntimeError):
    """The flip was asked for after requests already live in Postgres.

    A second flip would re-import SharePoint and reset Postgres statuses from it,
    reverting decisions made since the switch, so it is refused. It subclasses
    ``RuntimeError`` so the admin route maps it to the same 409 as a busy lock.
    """


async def flip_to_postgres() -> dict:
    """Run a final sync under the lock, pausing writes, then switch to Postgres.

    Returns:
        ``{"final_sync": <counts>, "storage": "postgres", "staff_storage": bool}``,
        where ``staff_storage`` says whether that setting also existed and was
        flipped.

    Raises:
        AlreadySwitched: When requests already live in Postgres (a second flip).
        RuntimeError: When a sync is already running and the lock cannot be taken.
    """
    if await requests_in_postgres():
        # Refuse a second flip: re-running the final sync would overwrite the
        # statuses of requests decided in Postgres with SharePoint's older values.
        raise AlreadySwitched("Already switched")
    lock = await acquire_sync_lock()                           # on its own connection, before any work
    if lock is None:
        raise RuntimeError("A sync is running; try the switch again in a moment.")
    await begin_pause()                                        # refuse approvals/submissions/staff writes now
    try:
        await asyncio.sleep(PAUSE_SETTLE_SECONDS)              # let in-flight decisions land in SharePoint first
        async with async_session() as session:
            run = SyncRun(job="final_sync")                    # started_at is the flip time the check schedules from
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
                raise
        # Only after the final sync is committed does the app start using Postgres.
        await set_setting(REQUESTS_STORAGE, POSTGRES)
        staff_flipped = STAFF_STORAGE in app_settings.DEFAULTS
        if staff_flipped:
            await set_setting(STAFF_STORAGE, POSTGRES)         # keep staff and requests on the same backend
        logger.info("Storage switched to Postgres (staff_storage flipped: %s)", staff_flipped)
        return {"final_sync": counts, "storage": POSTGRES, "staff_storage": staff_flipped}
    finally:
        # Resume writes and free the lock even if the final sync or the flip raised.
        await end_pause()
        await release_sync_lock(lock)


async def flip_back() -> dict:
    """Point requests (and staff, if present) back at SharePoint. Rehearsal only.

    No final sync is run: this undoes a rehearsal flip so the app resumes reading
    and writing SharePoint. It is not a production step.

    Returns:
        ``{"storage": "sharepoint", "staff_storage": bool}``.
    """
    await set_setting(REQUESTS_STORAGE, SHAREPOINT)
    staff_flipped = STAFF_STORAGE in app_settings.DEFAULTS
    if staff_flipped:
        await set_setting(STAFF_STORAGE, SHAREPOINT)
    logger.info("Storage flipped back to SharePoint (staff_storage flipped: %s)", staff_flipped)
    return {"storage": SHAREPOINT, "staff_storage": staff_flipped}
