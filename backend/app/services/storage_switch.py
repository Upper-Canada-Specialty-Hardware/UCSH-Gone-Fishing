"""Flip requests (and staff, when ready) from SharePoint to Postgres (#335).

The switch is one deliberate step: take the sync lock so no shadow sync is
running, run every sync step one last time so Postgres holds SharePoint's latest
state, record that final sync, then set the ``requests_storage`` app setting to
``postgres`` (and ``staff_storage`` too, if the staff work has added that
setting). From then on the app reads and writes requests in Postgres, and the
post-switch SharePoint check (``sharepoint_check``) watches the old lists for
anything that still lands there.

``flip_back`` reverses the settings for a rehearsal, so a rehearsal can flip,
look around, and flip back without leaving the app pointed at Postgres.
"""
import logging

from app.database import async_session
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.services import app_settings
from app.services.app_settings import POSTGRES, REQUESTS_STORAGE, SHAREPOINT, set_setting
from app.services.shadow_sync import _run_steps, acquire_sync_lock, release_sync_lock

logger = logging.getLogger(__name__)

STAFF_STORAGE = "staff_storage"         # the staff store builder adds this setting; flip it too when present


async def flip_to_postgres() -> dict:
    """Run a final sync under the lock, then switch requests (and staff) to Postgres.

    Returns:
        ``{"final_sync": <counts>, "storage": "postgres", "staff_storage": bool}``,
        where ``staff_storage`` says whether that setting also existed and was
        flipped.

    Raises:
        RuntimeError: When a sync is already running and the lock cannot be taken.
    """
    async with async_session() as session:
        if not await acquire_sync_lock(session):
            raise RuntimeError("A sync is running; try the switch again in a moment.")
        run = SyncRun(job="final_sync")                        # started_at is the flip time the check schedules from
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
        finally:
            await release_sync_lock(session)

    # Only after the final sync is committed does the app start using Postgres.
    await set_setting(REQUESTS_STORAGE, POSTGRES)
    staff_flipped = STAFF_STORAGE in app_settings.DEFAULTS
    if staff_flipped:
        await set_setting(STAFF_STORAGE, POSTGRES)             # keep staff and requests on the same backend
    logger.info("Storage switched to Postgres (staff_storage flipped: %s)", staff_flipped)
    return {"final_sync": counts, "storage": POSTGRES, "staff_storage": staff_flipped}


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
