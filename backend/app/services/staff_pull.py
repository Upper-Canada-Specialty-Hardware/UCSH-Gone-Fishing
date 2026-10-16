"""Pull edits made directly in the SharePoint Staff Directory into Postgres (#268).

People keep editing the SharePoint list during and after the move: HR fixes a
record there, a flow updates a field, someone corrects a balance. Those edits
have to reach Postgres, so this reads the list and, for each person, keeps
whichever side's edit is newer by modified time. When the SharePoint edit wins,
the typed row is updated and each changed field is written to ``staff_changes``
with source ``"sharepoint"`` so the history shows where the change came from.

A pulled edit is never queued for write-back, so a change that originated in
SharePoint is not echoed back to it. The pull is gated off in rehearsal mode,
the same as the write-back, so a rehearsal never touches the live list.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.models.staff import Staff
from app.models.staff_change import StaffChange
from app.repositories.postgres.staff import (
    _DATE_COLUMNS,
    _NUMBER_COLUMNS,
    _TEXT_COLUMNS,
    _num,
    _parse_dt,
    apply_sp_fields,
    staff_query,
    to_sp_fields,
)

logger = logging.getLogger(__name__)

# The columns a pulled change is logged against: the fields HR reads in the
# history. Links are still applied, but not diffed into the change log.
_LOGGED_COLUMNS = (
    list(_TEXT_COLUMNS) + list(_NUMBER_COLUMNS) + list(_DATE_COLUMNS) + ["Extension"]
)
_NUMBER_COLUMN_SET = set(_NUMBER_COLUMNS) | {"Extension"}


def _writes_allowed() -> bool:
    """Whether the pull may touch the live SharePoint list (off in rehearsal).

    A missing rehearsal module counts as allowed, so this works on its own until
    that work is joined.

    Returns:
        True when the pull may read and apply SharePoint edits.
    """
    try:
        from app.services.rehearsal import sharepoint_writes_allowed
    except ImportError:
        return True
    return sharepoint_writes_allowed()


def _aware(moment: datetime | None) -> datetime | None:
    """A stored timestamp as tz-aware UTC, since SQLite hands it back naive.

    Args:
        moment: A datetime read from the row, or None.

    Returns:
        The value as tz-aware UTC, or None.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _changed(column: str, old, new) -> bool:
    """Whether a column's value really moved, comparing numbers as numbers.

    Args:
        column: The SharePoint column name.
        old: The current value (from the rebuilt Postgres shape).
        new: The incoming SharePoint value.

    Returns:
        True when the two differ meaningfully.
    """
    if column in _NUMBER_COLUMN_SET:                       # 2 and "2.0" are the same number
        return _num(old) != _num(new)
    return str(old if old is not None else "").strip() != str(new if new is not None else "").strip()


def _diff(before_fields: dict, incoming: dict) -> list[tuple[str, object, object]]:
    """The logged columns that changed between the stored and the incoming record.

    Args:
        before_fields: The person's current SharePoint shape (rebuilt from Postgres).
        incoming: The SharePoint fields just read.

    Returns:
        One ``(column, old, new)`` per changed logged column.
    """
    changes: list[tuple[str, object, object]] = []
    for column in _LOGGED_COLUMNS:
        if column not in incoming:                         # a partial payload leaves a column alone
            continue
        old = before_fields.get(column)
        new = incoming.get(column)
        if _changed(column, old, new):
            changes.append((column, old, new))
    return changes


async def staff_pull_step(session: AsyncSession, sync_run) -> dict:
    """Apply newer SharePoint edits to the staff table, logging each change. No commit.

    For each SharePoint record: a person not yet in Postgres is imported; an
    existing person is updated only when the SharePoint edit is newer than the
    stored one (newer wins by modified time). Each applied field change is
    written to ``staff_changes`` with source "sharepoint", in this same session,
    so the log and the row move together. No write-back is queued, so there is no
    echo.

    Args:
        session: The caller's open session; the caller commits it.
        sync_run: The ``SyncRun`` row (or None in a test); unused beyond context.

    Returns:
        ``{"seen", "applied", "imported", "skipped_older", "changes_recorded",
        "skipped"}``; ``skipped`` is -1 when rehearsal mode turned the pull off.
    """
    if not _writes_allowed():                              # rehearsal: do not touch the live list
        logger.info("Staff pull skipped: SharePoint access is off (rehearsal mode)")
        return {"seen": 0, "applied": 0, "imported": 0, "skipped_older": 0,
                "changes_recorded": 0, "skipped": -1}

    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    counts = {"seen": 0, "applied": 0, "imported": 0, "skipped_older": 0,
              "changes_recorded": 0, "skipped": 0}

    for item in items:
        sp_id = str(item.get("id"))
        fields = dict(item.get("fields", {}))
        counts["seen"] += 1
        incoming_modified = _parse_dt(fields.get("Modified")) or _parse_dt(item.get("lastModifiedDateTime"))

        row = (await session.execute(
            staff_query().where(Staff.sp_item_id == sp_id)
        )).scalars().first()

        if row is None:                                    # appeared in SharePoint after the import
            row = Staff(sp_item_id=sp_id)
            session.add(row)
            await apply_sp_fields(session, row, fields)
            row.sp_modified = incoming_modified or utcnow()
            counts["imported"] += 1
            continue

        # Newer wins: skip when the stored edit is as new as or newer than this.
        stored_modified = _aware(row.sp_modified)          # SQLite returns it naive
        if incoming_modified is not None and stored_modified is not None and incoming_modified <= stored_modified:
            counts["skipped_older"] += 1
            continue

        before_fields = to_sp_fields(row)                  # for the change log, before applying
        name_now = (fields.get("Title") or before_fields.get("Title") or "").strip()
        changes = _diff(before_fields, fields)
        await apply_sp_fields(session, row, fields)
        row.sp_modified = incoming_modified or utcnow()
        counts["applied"] += 1
        for column, old, new in changes:                   # one history row per changed field
            session.add(StaffChange(
                employee_id=sp_id,
                employee_name=name_now,
                field=column,
                old_value=old,
                new_value=new,
                reason="Pulled from the SharePoint Staff Directory.",
                source="sharepoint",
            ))
            counts["changes_recorded"] += 1

    return counts
