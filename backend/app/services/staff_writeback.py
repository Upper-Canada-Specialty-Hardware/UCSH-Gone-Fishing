"""Replay app-made staff changes onto the SharePoint Staff Directory list (#267).

While and after the Staff Directory moves to Postgres, other systems keep
reading the SharePoint list: the Bulletin Board views, the Power Automate flows,
the Microsoft Form and any other app pointed at it. So every change the app
makes to a person's record is queued in ``staff_writeback`` and written back to
SharePoint in the background, retried with a growing delay until it lands. The
user is never blocked on SharePoint: the change is committed to Postgres and the
replay happens after.

A pulled edit (#268) is never queued, so a change that originated in SharePoint
is not echoed back to it. The whole replay is gated off in rehearsal mode (no
live SharePoint writes); a missing rehearsal module counts as "allowed".
"""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.models.staff import Staff, StaffWriteback

logger = logging.getLogger(__name__)

# Statuses a row moves through; see the model for their meaning.
PENDING = "pending"
SENDING = "sending"
DONE = "done"
FAILED = "failed"
DROPPED = "dropped"

# Retry backoff: the delay before the next try grows with the attempt count,
# capped so a stuck row still retries roughly hourly rather than never.
_BACKOFF_CAP_SECONDS = 3600


def _backoff_seconds(attempts: int) -> int:
    """How long to wait before the next try, growing with the attempt count.

    Args:
        attempts: How many tries have already failed.

    Returns:
        The delay in seconds: 2, 4, 8, ... capped at one hour.
    """
    return min(2 ** max(attempts, 1), _BACKOFF_CAP_SECONDS)


def _writes_allowed() -> bool:
    """Whether SharePoint writes are allowed now (off in rehearsal mode).

    The rehearsal module is owned by another part of the migration and may not
    be present yet; a missing module counts as "allowed", so this works on its
    own until that work is joined.

    Returns:
        True when the drain may write to SharePoint.
    """
    try:
        from app.services.rehearsal import sharepoint_writes_allowed
    except ImportError:
        return True                                        # rehearsal not wired yet: allow
    return sharepoint_writes_allowed()


def enqueue_writeback(
    session: AsyncSession, sp_item_id: str, fields: dict, op: str = "update"
) -> StaffWriteback:
    """Queue one SharePoint-shaped change to replay onto a staff item. No commit.

    Added to the caller's session so the queue row commits with the change that
    caused it (or not at all). Never called for a pulled edit, so a SharePoint
    change is never echoed back.

    Args:
        session: The caller's open session; the caller commits it.
        sp_item_id: The staff row's id the change belongs to (the Postgres-minted
            id for a native person, the SharePoint id for an imported one).
        fields: The SharePoint column -> value patch (or, for a create, the new
            item's fields) to write.
        op: ``"update"`` to patch the item (the default), ``"create"`` to create
            the SharePoint item for a Postgres-native person and store its returned
            id back on the staff row.

    Returns:
        The queued StaffWriteback row (not yet committed).
    """
    row = StaffWriteback(
        sp_item_id=str(sp_item_id),
        fields=dict(fields),                               # a copy: the caller may mutate theirs
        op=op,
        status=PENDING,
        attempts=0,
        next_attempt_at=utcnow(),                          # due immediately
    )
    session.add(row)
    return row


async def _target_item_id(session: AsyncSession, row: StaffWriteback) -> str:
    """The SharePoint item id to patch for an update replay.

    An imported person's own id already is the SharePoint id. A Postgres-native
    person's real SharePoint id is only known once the create replay has run and
    stored it (sp_remote_item_id), so until then the patch has nowhere to land and
    falls back to the minted id (which fails and retries, by which time the create
    has usually filled the real id).

    Args:
        session: The open session.
        row: The write-back row being drained.

    Returns:
        The SharePoint item id to patch.
    """
    staff_row = (await session.execute(
        select(Staff).where(Staff.sp_item_id == row.sp_item_id)
    )).scalars().first()
    if staff_row is not None and staff_row.sp_remote_item_id:
        return staff_row.sp_remote_item_id                 # the real SharePoint id, once known
    return row.sp_item_id                                  # imported id, or minted id before create


async def drain_writebacks(limit: int = 50) -> dict:
    """Write every due queued change to SharePoint, retrying failures later.

    Picks the due rows (pending or previously failed, next_attempt_at reached),
    patches each onto its SharePoint item, and marks it done or schedules a later
    retry with a growing delay. Safe to run again: a done row is never re-sent,
    and a row that keeps failing keeps its place in the queue.

    Args:
        limit: The most rows to attempt in one pass.

    Returns:
        ``{"sent", "failed", "skipped"}`` counts; ``skipped`` is non-zero only
        when rehearsal mode turned the replay off.
    """
    if not _writes_allowed():                              # rehearsal: leave the queue untouched
        logger.info("Staff write-back skipped: SharePoint writes are off (rehearsal mode)")
        return {"sent": 0, "failed": 0, "skipped": -1}

    now = utcnow()
    async with async_session() as session:
        due = (await session.execute(
            select(StaffWriteback)
            .where(
                StaffWriteback.status.in_([PENDING, FAILED, SENDING]),
                (StaffWriteback.next_attempt_at.is_(None)) | (StaffWriteback.next_attempt_at <= now),
            )
            .order_by(StaffWriteback.next_attempt_at.asc(), StaffWriteback.id.asc())
            .limit(limit)
        )).scalars().all()

        sent = 0
        failed = 0
        for row in due:
            try:
                if row.op == "create":
                    # A Postgres-native person: create the SharePoint item and
                    # store its returned id on the staff row, so this person's
                    # later patches and the pull resolve the same SharePoint item.
                    created = await sp_client.create_list_item(
                        settings.SP_LIST_STAFF_DIRECTORY, dict(row.fields or {}),
                    )
                    real_id = str(created.get("id") or "")
                    staff_row = (await session.execute(
                        select(Staff).where(Staff.sp_item_id == row.sp_item_id)
                    )).scalars().first()
                    if staff_row is not None and real_id:
                        staff_row.sp_remote_item_id = real_id   # map minted id -> real SharePoint id
                else:
                    target = await _target_item_id(session, row)  # the real id once known
                    await sp_client.update_list_item_fields(
                        settings.SP_LIST_STAFF_DIRECTORY, target, dict(row.fields or {}),
                    )
            except Exception as exc:                       # keep the row, retry it later
                row.attempts += 1
                row.status = FAILED
                row.last_error = str(exc)
                row.next_attempt_at = utcnow() + timedelta(seconds=_backoff_seconds(row.attempts))
                failed += 1
                logger.warning("Staff write-back for %s failed (attempt %s): %s",
                               row.sp_item_id, row.attempts, exc)
                continue
            row.status = DONE                              # landed
            row.attempts += 1
            row.sent_at = utcnow()
            row.last_error = None
            sent += 1
        await session.commit()
    return {"sent": sent, "failed": failed, "skipped": 0}
