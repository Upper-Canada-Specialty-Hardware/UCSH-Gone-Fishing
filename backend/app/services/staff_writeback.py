"""Replay app-made staff changes onto the SharePoint Staff Directory list (#267).

While and after the Staff Directory moves to Postgres, other systems keep
reading the SharePoint list: the Bulletin Board views, the Power Automate flows,
the Microsoft Form and any other app pointed at it. So every change the app
makes to a person's record is queued in ``staff_writeback`` and written back to
SharePoint in the background, retried with a growing delay until it lands. The
user is never blocked on SharePoint: the change is committed to Postgres and the
replay happens after.

The drain claims the rows it is about to send before it calls SharePoint, so two
app instances never replay the same row (and never create the SharePoint item for
one person twice). A row is claimed by moving it to ``sending`` and pushing its
``next_attempt_at`` one claim-timeout ahead, committed before any Graph call; on
Postgres the pick is ``FOR UPDATE SKIP LOCKED`` so a second drain running at the
same moment steps over the claimed rows. A claim older than the timeout is treated
as a crashed instance's and taken again, so a crash never parks a change forever.

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

# A claimed ("sending") row whose claim is older than this is treated as stranded
# by a crashed instance and may be claimed again, so a crash never parks a change
# forever. The claim stamps ``next_attempt_at`` this far ahead, so a healthy
# in-flight row is not re-picked while its one owner is still working on it; this
# column doubles as the claim expiry, so no extra column is needed.
_CLAIM_TIMEOUT_SECONDS = 600                               # ten minutes


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


async def _claim_due_rows(session: AsyncSession, now, limit: int) -> list[StaffWriteback]:
    """Take up to ``limit`` due rows for this drain alone, committing the claim.

    A due row is a pending one, a failed one whose backoff has elapsed, or a
    ``sending`` one whose claim has expired (its owning instance crashed). On
    Postgres the pick is ``FOR UPDATE SKIP LOCKED`` so a second drain running at
    the same moment never takes the same rows. Each taken row is moved to
    ``sending`` with ``next_attempt_at`` pushed one claim-timeout ahead, and the
    claim is committed before any SharePoint call, so any later drain sees the
    claim (a future ``next_attempt_at``) and leaves the row alone.

    Args:
        session: The open session; this function commits the claim on it.
        now: The reference time for "due" and for the claim window.
        limit: The most rows to claim in one pass.

    Returns:
        The claimed rows, already committed as ``sending`` (their attributes stay
        readable because the session does not expire on commit).
    """
    statement = (
        select(StaffWriteback)
        .where(
            StaffWriteback.status.in_([PENDING, FAILED, SENDING]),
            (StaffWriteback.next_attempt_at.is_(None)) | (StaffWriteback.next_attempt_at <= now),
        )
        .order_by(StaffWriteback.next_attempt_at.asc(), StaffWriteback.id.asc())
        .limit(limit)
    )
    if session.bind.dialect.name == "postgresql":          # a real claim only where it exists
        statement = statement.with_for_update(skip_locked=True)
    rows = (await session.execute(statement)).scalars().all()
    claim_until = now + timedelta(seconds=_CLAIM_TIMEOUT_SECONDS)   # reclaim only after this
    for row in rows:
        row.status = SENDING                               # in flight: other drains skip it
        row.next_attempt_at = claim_until                  # not due again until the claim expires
    await session.commit()                                 # publish the claim before any SharePoint call
    return rows


async def _replay_create(session: AsyncSession, row: StaffWriteback) -> None:
    """Create the SharePoint item for a Postgres-native person, at most once.

    Before creating, the staff row's ``sp_remote_item_id`` is re-read under this
    drain's claim: when it is already set the SharePoint item exists (an earlier
    run or another instance made it), so no second item is created and the replay
    just finishes. Otherwise the item is created and its returned id is stored on
    the staff row, so later patches and the pull resolve the same SharePoint item.

    Args:
        session: The open session; the id mapping is written, the caller commits.
        row: The claimed ``create`` write-back row.

    Raises:
        Exception: Any SharePoint error, so the caller marks the row failed and
            retries it later.
    """
    staff_row = (await session.execute(
        select(Staff).where(Staff.sp_item_id == row.sp_item_id)
    )).scalars().first()
    if staff_row is not None and staff_row.sp_remote_item_id:
        return                                             # already created: never make a second item
    created = await sp_client.create_list_item(
        settings.SP_LIST_STAFF_DIRECTORY, dict(row.fields or {}),
    )
    real_id = str(created.get("id") or "")
    if staff_row is not None and real_id:
        staff_row.sp_remote_item_id = real_id              # map minted id -> real SharePoint id


async def drain_writebacks(limit: int = 50) -> dict:
    """Write every due queued change to SharePoint, retrying failures later.

    Claims the due rows first (committing the claim so no other instance replays
    them), then patches or creates each SharePoint item, and marks each row done
    or schedules a later retry with a growing delay. Safe to run again and safe to
    run beside another instance: a done row is never re-sent, a row that keeps
    failing keeps its place in the queue, and a crashed claim is retaken after the
    claim timeout.

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
        due = await _claim_due_rows(session, now, limit)   # claimed and committed as "sending"

        sent = 0
        failed = 0
        for row in due:
            try:
                if row.op == "create":
                    # A Postgres-native person: create the SharePoint item and
                    # store its returned id on the staff row, guarded so a retry
                    # or a second drain never makes a duplicate item.
                    await _replay_create(session, row)
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
                await session.commit()                     # record the outcome before the next row
                continue
            row.status = DONE                              # landed
            row.attempts += 1
            row.sent_at = utcnow()
            row.last_error = None
            row.next_attempt_at = None                     # nothing more to do for this row
            sent += 1
            await session.commit()                         # record the outcome before the next row
    return {"sent": sent, "failed": failed, "skipped": 0}
