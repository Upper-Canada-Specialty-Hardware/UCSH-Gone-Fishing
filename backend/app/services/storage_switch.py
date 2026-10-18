"""Flip requests (and staff) from SharePoint to Postgres (#335).

The switch is one deliberate step, run in the background so the request that
triggers it returns at once. Under the sync lock (so no shadow sync overlaps) it:
pauses request and staff writes, waits for any write that was already in flight to
finish, runs every sync step one last time so Postgres holds SharePoint's latest
state, records that final sync, and only then sets both the ``requests_storage``
and ``staff_storage`` app settings to ``postgres`` in one transaction. From then
on the app reads and writes requests and staff in Postgres, and the post-switch
SharePoint check watches the old lists for anything that still lands there.

The flip refuses to change the settings when the final sync reported any error
(an item that would not import, or a truncated SharePoint read), when the pause
lapsed while it ran, or when an in-flight write did not finish in time. In every
such case the pause is cleared, the settings are left unchanged, and the switch is
marked failed with the reason (and any error ids) so an admin can see what
happened on the storage screen. The pause is cleared in ``finally`` too, so writes
resume even if the flip raises, and it carries its own expiry so a crash never
leaves the app paused (see ``switch_pause``).

``flip_back`` reverses the settings for a rehearsal, so a rehearsal can flip, look
around, and flip back without leaving the app pointed at Postgres.
"""
import asyncio
import logging

from app.database import async_session
from app.models.app_setting import AppSetting
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.repositories.factory import holidays_in_postgres
from app.repositories.request_store import requests_in_postgres
from app.repositories.staff_store import staff_in_postgres
from app.services.app_settings import (
    HOLIDAYS_STORAGE,
    POSTGRES,
    REQUESTS_STORAGE,
    SHAREPOINT,
    STAFF_STORAGE,
    set_setting,
)
from app.services.shadow_sync import SYNC_STEPS, acquire_sync_lock, release_sync_lock
from app.services.switch_pause import begin_pause, end_pause, is_paused, wait_until_idle

logger = logging.getLogger(__name__)

# The shortest the switch settles after pausing before it reads SharePoint, so a
# write that had just started lands first. The in-flight wait does the real work;
# this is only a floor. Tests monkeypatch it to zero so they do not sleep.
PAUSE_SETTLE_SECONDS = 2

# The longest the switch waits for in-flight writes to finish before it gives up,
# ends the pause and fails the switch rather than reading a half-written balance.
IN_FLIGHT_TIMEOUT = 120


class AlreadySwitched(RuntimeError):
    """The flip was asked for after requests and staff already live in Postgres.

    A second flip would re-import SharePoint and reset Postgres statuses from it,
    reverting decisions made since the switch, so it is refused. It subclasses
    ``RuntimeError`` so the admin route maps it to the same 409 as a busy lock.
    """


class SwitchRunning(RuntimeError):
    """A flip was asked for while one is already running (a second POST)."""


# The in-memory switch status, shown on the storage screen. One backend instance
# runs the switch, so a process-local dict is enough; it is reset on each start.
_SWITCH_STATUS: dict = {
    "state": "idle",            # idle / running / done / failed
    "step": None,               # pausing / waiting_in_flight / final_sync / switching / done
    "started_at": None,         # ISO time the flip began
    "finished_at": None,        # ISO time it ended (done or failed)
    "error": None,              # why it failed, if it did
    "final_sync": None,         # the final sync's per-step counts (errors and ids included)
    "summary": None,            # what Postgres holds once the switch is done (see _switch_summary)
}


def switch_status() -> dict:
    """The current switch status, for the storage screen.

    Returns:
        A copy of the status dict (state, step, times, error, final_sync).
    """
    return dict(_SWITCH_STATUS)


def switch_is_running() -> bool:
    """Whether a flip is running right now.

    Returns:
        True while the background switch task is in progress.
    """
    return _SWITCH_STATUS["state"] == "running"


def _status_begin() -> None:
    """Mark the switch as started, clearing any previous run's result."""
    _SWITCH_STATUS.update(
        state="running", step="pausing", started_at=utcnow().isoformat(),
        finished_at=None, error=None, final_sync=None, summary=None,
    )
    logger.info("Switch: started")                             # every switch log line starts "Switch:"


def _status_step(step: str) -> None:
    """Record the phase the switch is in now."""
    _SWITCH_STATUS["step"] = step
    logger.info("Switch: step %s", step)                       # one line per phase, to follow it live


def _status_done() -> None:
    """Mark the switch finished and successful."""
    _SWITCH_STATUS.update(state="done", step="done", finished_at=utcnow().isoformat())
    logger.info("Switch: complete")


def _status_fail(message: str, error_ids: list | None = None) -> None:
    """Mark the switch failed, with a reason and any offending item ids.

    Args:
        message: The plain reason the flip was refused or failed.
        error_ids: The SharePoint item ids the final sync could not import, if any.
    """
    if error_ids:
        message = f"{message} (item ids: {', '.join(str(i) for i in error_ids)})"
    _SWITCH_STATUS.update(state="failed", error=message, finished_at=utcnow().isoformat())
    logger.error("Switch: FAILED, nothing was switched: %s", message)


async def _all_in_postgres() -> bool:
    """Whether requests, staff AND holidays already live in Postgres (switch done).

    Returns:
        True only when all three backends are Postgres; with any one still on
        SharePoint the switch has work left, so this is False.
    """
    return (
        await requests_in_postgres()
        and await staff_in_postgres()
        and await holidays_in_postgres()
    )


def _sync_errors(counts: dict) -> tuple[int, list]:
    """Total error/failed counts and the offending item ids across every step.

    Reads both step shapes: the request import's per-kind ``errors``/``error_ids``
    and the staff import's top-level ``failed``.

    Args:
        counts: The final sync's per-step counts.

    Returns:
        ``(total_errors, error_ids)``; zero and an empty list when the sync was clean.
    """
    total = 0
    ids: list = []
    for step_counts in counts.values():
        if not isinstance(step_counts, dict):
            continue
        failed = step_counts.get("failed")                     # the staff step's failure count
        if isinstance(failed, int):
            total += failed
        for value in step_counts.values():                     # the request step's per-kind blocks
            if isinstance(value, dict):
                errors = value.get("errors")
                if isinstance(errors, int):
                    total += errors
                ids.extend(value.get("error_ids") or [])
    return total, ids


async def _run_final_sync() -> dict:
    """Run every sync step one last time, keeping the pause alive between steps.

    Writes one ``final_sync`` SyncRun row, whose ``started_at`` records when the
    switch happened. Each step runs in its own session and commits,
    and the pause expiry is refreshed before each step; refreshing between steps
    (not inside an open step transaction) keeps the pause in force through a slow
    sync without a write of one step blocking the pause refresh of the next.

    Returns:
        The per-step counts.

    Raises:
        Exception: Whatever a step raises (a truncated read, say); the SyncRun row
            records the error first, then it propagates so the flip is refused.
    """
    # Record the run first and commit it, so its id and start time are fixed before
    # the steps run.
    async with async_session() as session:
        run = SyncRun(job="final_sync")
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    counts: dict = {}
    try:
        for step in SYNC_STEPS:
            await begin_pause()                                # refresh the pause expiry: keep it in force
            async with async_session() as session:             # one session per step, committed on its own
                run = await session.get(SyncRun, run_id)
                counts[step.__name__] = await step(session, run)
                await session.commit()
            logger.info("Switch: final sync %s: %s", step.__name__, counts[step.__name__])
        async with async_session() as session:
            run = await session.get(SyncRun, run_id)
            run.counts = counts
            run.finished_at = utcnow()
            await session.commit()
        return counts
    except Exception as e:
        async with async_session() as session:
            run = await session.get(SyncRun, run_id)
            run.error = str(e)
            run.counts = counts                                # whatever landed before the failure
            run.finished_at = utcnow()
            await session.commit()
        raise


async def _outbox_count() -> int:
    """How many email/text messages the outbox holds, of any status.

    Returns:
        The row count; read before and after the switch so the summary can show
        that the switch itself queued nothing.
    """
    from sqlalchemy import func, select
    from app.models.outbox import Outbox                       # lazy: keeps this module's imports light
    async with async_session() as session:
        return int((await session.execute(select(func.count()).select_from(Outbox))).scalar_one())


async def _switch_summary(outbox_before: int) -> dict:
    """Count what Postgres holds right after the flip, and log it in one line.

    This is the line to read in the logs to confirm the switch went through: the
    request rows per kind and the pending ones, the staff and holiday rows, the
    manager links (and how many name nobody on staff), and how many messages
    the switch queued, which must be zero.

    Args:
        outbox_before: The outbox row count taken before the pause began.

    Returns:
        The counts, also kept on the switch status for the storage card.
    """
    from sqlalchemy import func, select
    from app.models.holiday import Holiday
    from app.models.request_record import RequestRecord
    from app.models.staff import Staff, StaffSupervisor
    async with async_session() as session:
        by_kind = dict((await session.execute(
            select(RequestRecord.kind, func.count()).group_by(RequestRecord.kind)
        )).all())
        pending = dict((await session.execute(
            select(RequestRecord.kind, func.count())
            .where(RequestRecord.status == "pending").group_by(RequestRecord.kind)
        )).all())
        staff = (await session.execute(select(func.count()).select_from(Staff))).scalar_one()
        holidays = (await session.execute(select(func.count()).select_from(Holiday))).scalar_one()
        links = (await session.execute(select(func.count()).select_from(StaffSupervisor))).scalar_one()
        unlinked = (await session.execute(
            select(func.count()).select_from(StaffSupervisor)
            .where(StaffSupervisor.manager_sp_user_id.is_(None))
        )).scalar_one()
    summary = {
        "requests": {k: int(v) for k, v in by_kind.items()},
        "pending": {k: int(v) for k, v in pending.items()},
        "staff": int(staff),
        "holidays": int(holidays),
        "manager_links": int(links),
        "manager_links_not_on_staff": int(unlinked),
        "messages_queued_by_switch": await _outbox_count() - outbox_before,
    }
    logger.info("Switch: Postgres now holds %s", summary)
    if summary["messages_queued_by_switch"]:
        logger.error("Switch: messages were queued during the switch: %s", summary["messages_queued_by_switch"])
    return summary


def _staff_number(row) -> int | None:
    """A staff row's id as the number the manager pickers use, or None.

    Args:
        row: A Staff row.

    Returns:
        ``int(sp_item_id)``, or None when the id is not a number.
    """
    try:
        return int(row.sp_item_id)
    except (TypeError, ValueError):
        return None


async def _rekey_people_links(session) -> dict:
    """Point every manager and person link at a staff id instead of a Microsoft 365 id.

    Imported staff rows name their managers (and their own account link) by the
    Microsoft 365 user id SharePoint stored. After the switch the app has no
    SharePoint user list, and the manager pickers, the assignment screen and the
    bulk tools all identify a manager by their staff id. Left alone, the two kinds
    of small numbers mix: keeping an imported manager while editing a person
    would look their id up as a staff id and save a blank or wrong name, and team
    membership is matched by that name. So, in the same transaction as the flip:

    * each manager or supervisor link takes the staff id of the person it names:
      the linked staff row when the import found one, else the staff row with the
      same name (the rule the rest of the app matches managers by);
    * the supervisor link, which SharePoint stores with no name, is matched by the
      record's own Supervisor text column;
    * a link whose person is not in the staff table keeps its name and loses its
      id, so it can never be mistaken for somebody's staff id;
    * each person's own account link becomes their own staff id.

    Rerunning it gives the same result, since it matches by the linked row or name.

    Args:
        session: The flip's open session (committed by the caller).

    Returns:
        ``{"links": n, "unmatched": n}`` for the log line.
    """
    from sqlalchemy import select
    from app.models.staff import Staff, StaffSupervisor          # lazy: keeps this module's imports light
    staff_rows = (await session.execute(select(Staff).order_by(Staff.id))).scalars().all()
    by_key = {row.id: row for row in staff_rows}                  # internal key -> row
    by_name: dict[str, Staff] = {}
    for row in staff_rows:
        name = (row.name or "").strip().lower()
        if name:
            by_name.setdefault(name, row)                         # first wins on a duplicate name
    for row in staff_rows:
        row.title_link_sp_user_id = _staff_number(row)            # their own link: their own staff id
    links = (await session.execute(select(StaffSupervisor))).scalars().all()
    unmatched = 0
    for link in links:
        name = link.manager_name
        if not name and link.relation == "supervisor_link":
            # SharePoint gives the supervisor link an id but no name; the record's
            # own Supervisor text column names the same person.
            owner = by_key.get(link.staff_id)
            name = owner.supervisor_text if owner is not None else None
        target = by_key.get(link.manager_staff_id) if link.manager_staff_id else None
        if target is None:
            target = by_name.get((name or "").strip().lower())   # match by name
        if target is None:
            link.manager_sp_user_id = None                        # not on staff: keep the name, drop the id
            unmatched += 1
            continue
        link.manager_sp_user_id = _staff_number(target)           # the id the pickers use from now on
        link.manager_staff_id = target.id
        if not link.manager_name:
            link.manager_name = target.name                       # fill a blank name from the row
    return {"links": len(links), "unmatched": unmatched}


async def _flip_settings_to_postgres() -> None:
    """Set all three storage settings to Postgres in one transaction (#335 fix).

    Writing requests, staff and holidays together means the app never sees one
    domain in Postgres while another is still SharePoint because of a crash
    between separate commits. The staff manager links are moved to staff ids in
    the same transaction (see ``_rekey_people_links``).
    """
    async with async_session() as session:
        rekeyed = await _rekey_people_links(session)          # before the flip lands, same commit
        logger.info("Switch: manager links moved to staff ids: %s", rekeyed)
        for key in (REQUESTS_STORAGE, STAFF_STORAGE, HOLIDAYS_STORAGE):
            row = await session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value=POSTGRES))   # first change from the default
            else:
                row.value = POSTGRES
        await session.commit()
    logger.info("Storage switched to Postgres (requests, staff and holidays)")


async def flip_to_postgres() -> dict:
    """Run the final sync under the lock, pausing writes, then switch to Postgres.

    Returns:
        ``{"final_sync": <counts>, "storage": "postgres", "staff_storage": True}``.

    Raises:
        AlreadySwitched: When both backends already live in Postgres.
        RuntimeError: When a sync is already running, an in-flight write did not
            finish in time, the final sync reported errors, or the pause lapsed; in
            each case the pause is cleared and the settings are left unchanged.
    """
    if await _all_in_postgres():
        # Refuse a second flip: re-running the final sync would overwrite the
        # statuses of requests decided in Postgres with SharePoint's older values.
        raise AlreadySwitched("Already switched")
    _status_begin()
    lock = await acquire_sync_lock()                           # on its own connection, before any work
    if lock is None:
        _status_fail("A sync is running; try the switch again in a moment.")
        raise RuntimeError("A sync is running; try the switch again in a moment.")
    try:
        # Re-check under the lock: a flip on another path may have finished between
        # the pre-check and taking the lock (#335 fix, finding 4d).
        if await _all_in_postgres():
            _status_done()
            raise AlreadySwitched("Already switched")

        outbox_before = await _outbox_count()                  # to prove the switch queues no message
        await begin_pause()                                    # refuse approvals/submissions/staff writes now
        _status_step("waiting_in_flight")
        await asyncio.sleep(max(0, PAUSE_SETTLE_SECONDS))      # a short floor before the in-flight wait
        if not await wait_until_idle(IN_FLIGHT_TIMEOUT):
            # A write that started before the pause is still running; reading now
            # could miss its SharePoint write, so give up rather than risk a loss.
            await end_pause()
            msg = "in-flight writes did not finish in time; the switch was aborted"
            _status_fail(msg)
            raise RuntimeError(msg)

        _status_step("final_sync")
        counts = await _run_final_sync()                       # may raise (truncated read, bad item)
        _SWITCH_STATUS["final_sync"] = counts
        total_errors, error_ids = _sync_errors(counts)
        if total_errors > 0:
            # Some item would not import; flipping now would lose it, so refuse.
            await end_pause()
            msg = f"the final sync reported {total_errors} error(s); the switch was aborted"
            _status_fail(msg, error_ids)
            raise RuntimeError(msg)

        _status_step("switching")
        if not await is_paused():
            # The pause lapsed while the final sync ran, so a write could have
            # landed in SharePoint after the read; refuse rather than flip blind.
            await end_pause()
            msg = "the switch pause lapsed before the flip; the switch was aborted"
            _status_fail(msg)
            raise RuntimeError(msg)

        # Stop SharePoint calling this app before Postgres becomes the source:
        # after the flip no SharePoint call is allowed, deletes included.
        from app.tasks.subscription_manager import delete_all_subscriptions
        await delete_all_subscriptions()
        await _flip_settings_to_postgres()                     # all three settings, one transaction
        summary = await _switch_summary(outbox_before)         # what landed, for the log and the card
        _SWITCH_STATUS["summary"] = summary
        _status_done()
        return {"final_sync": counts, "storage": POSTGRES, "staff_storage": True, "holidays_storage": True}
    except AlreadySwitched:
        raise                                                  # a refusal, not a failure; status already set
    except Exception as e:
        if _SWITCH_STATUS["state"] != "failed":                # an unexpected error (e.g. a step raised)
            _status_fail(str(e))
        raise
    finally:
        # Resume writes and free the lock even if the final sync or the flip raised.
        await end_pause()
        await release_sync_lock(lock)


async def _run_switch_task() -> None:
    """Run the flip as a background task, swallowing its result and error.

    ``flip_to_postgres`` has already recorded success or failure in the status, so
    this only keeps the task from raising an unretrieved exception.
    """
    try:
        await flip_to_postgres()
    except Exception:
        logger.exception("Storage switch failed")              # the reason is already in the status


async def start_switch() -> asyncio.Task:
    """Validate, then start the flip in the background and return its task.

    Returns:
        The created asyncio task running the flip.

    Raises:
        AlreadySwitched: When both backends already live in Postgres.
        SwitchRunning: When a flip is already running.
    """
    if await _all_in_postgres():                               # nothing to do; a second flip is refused
        raise AlreadySwitched("Already switched")
    # The running-check and the status set happen with no await between them, so
    # two near-simultaneous POSTs cannot both start a switch.
    if switch_is_running():
        raise SwitchRunning("The switch is already running.")
    _status_begin()
    return asyncio.create_task(_run_switch_task())


async def flip_back() -> dict:
    """Point requests, staff and holidays back at SharePoint. Rehearsal only.

    No final sync is run: this undoes a rehearsal flip so the app resumes reading
    and writing SharePoint. It is not a production step.

    Returns:
        ``{"storage": "sharepoint", "staff_storage": True, "holidays_storage": True}``.
    """
    await set_setting(REQUESTS_STORAGE, SHAREPOINT)
    await set_setting(STAFF_STORAGE, SHAREPOINT)
    await set_setting(HOLIDAYS_STORAGE, SHAREPOINT)
    logger.info("Storage flipped back to SharePoint (requests, staff and holidays)")
    return {"storage": SHAREPOINT, "staff_storage": True, "holidays_storage": True}
