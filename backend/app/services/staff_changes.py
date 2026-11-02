"""Record and read the history of changes HR makes to a person's numbers.

The admin dashboard calls ``record_changes`` whenever it saves an edit to a
staff record, one entry per field that moved, and ``list_changes`` to show that
person's history. The rows live in the staff_changes table and are only ever
added: this module never updates or deletes them.

A change batch is written all-or-nothing, so a dashboard save either records
every field it changed or none of them, never a half-written history.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.staff_change import StaffChange

logger = logging.getLogger(__name__)

# Actor labels for the two app channels that have no per-person identity: the
# admin dashboard shares one password (so "HR"), and an automatic change has no
# person behind it (so "system"). A manager edit records the manager's own name.
ADMIN_ACTOR = "HR"
SYSTEM_ACTOR = "system"
# The source the SharePoint version-history import writes under; its oldest
# version per person seeds the starting-value rows the profile marks (#448).
SHAREPOINT_SOURCE = "sharepoint"


def _as_version_number(version) -> float | None:
    """A version label like "2.0" as a float, or None when it is not numeric.

    Args:
        version: The stored ``version`` label (e.g. "1.0"), or None.

    Returns:
        The label as a float, or None for a blank or non-numeric label.
    """
    if version in (None, ""):                                  # app edits carry no version
        return None
    try:
        return float(version)                                  # "2.0" -> 2.0
    except (TypeError, ValueError):
        return None                                            # an odd label never crashes the read


def _start_versions(rows) -> dict[str, float]:
    """The oldest imported SharePoint version number per person in ``rows``.

    The import seeds a person's starting-value rows from their oldest kept
    SharePoint version; this finds that version number for each person so a reader
    can mark exactly those seed rows. A later blank->value change also has a null
    old value, so a version check, not the null old value alone, is what tells a
    seed apart (#448).

    Args:
        rows: StaffChange rows, for one person or many.

    Returns:
        ``{employee_id: oldest version number}`` for the people with imported rows.
    """
    starts: dict[str, float] = {}
    for r in rows:
        if r.source != SHAREPOINT_SOURCE:                      # only imported rows carry a version
            continue
        v = _as_version_number(r.version)
        if v is None:
            continue
        cur = starts.get(r.employee_id)
        if cur is None or v < cur:                             # keep the smallest (oldest) per person
            starts[r.employee_id] = v
    return starts


def _is_start_row(row, starts: dict[str, float]) -> bool:
    """Whether one row is a starting-value seed (its person's oldest import).

    Args:
        row: A StaffChange row.
        starts: ``_start_versions`` output.

    Returns:
        True for an imported row with no "before" at its person's oldest version.
    """
    if row.source != SHAREPOINT_SOURCE or row.old_value is not None:
        return False                                           # not imported, or a real before->after change
    start = starts.get(row.employee_id)
    return start is not None and _as_version_number(row.version) == start


def _to_utc_iso(moment: datetime) -> str:
    """Render a stored timestamp as an ISO 8601 string in UTC.

    SQLite hands timestamps back naive, Postgres hands them back tz-aware, so
    normalise both to UTC before formatting for a stable, comparable string.

    Args:
        moment: The changed_at value read from a row.

    Returns:
        An ISO 8601 string in UTC, e.g. "2026-10-14T13:05:00+00:00".
    """
    if moment.tzinfo is None:                                   # SQLite: assume the stored value is UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()         # aware -> UTC, then format


async def record_changes(
    employee_id: str,
    employee_name: str,
    changes: list[tuple[str, object, object]],   # (SharePoint column, old value, new value)
    reason: str,
    source: str = "admin_dashboard",
    actor: str | None = None,
) -> int:
    """Add one history row per changed field, all in one transaction.

    Args:
        employee_id: Staff Directory item id of the person; stored as a string.
        employee_name: Their name at the time, so the history reads well later.
        changes: One (field, old_value, new_value) tuple per field that moved;
            the values may be numbers, strings or None (JSON-stored).
        reason: Why HR made the change; required on every row written here (an
            app edit always has one; the SharePoint import writes its own rows).
        source: Where the change came from; "admin_dashboard" by default.
        actor: Who or what made the change ("HR", a manager's name, "system");
            None leaves the reader to fall back to ``source``.

    Returns:
        How many rows were added; 0 when ``changes`` is empty.

    Raises:
        ValueError: When ``reason`` is blank, so no app-recorded row lacks one.
    """
    if not reason or not reason.strip():                        # guard first -> blank reason never writes
        raise ValueError("A reason is required for a staff change.")
    if not changes:                                            # nothing to record -> skip the database entirely
        return 0
    employee_id = str(employee_id)                             # id is always compared and stored as a string
    rows = [
        StaffChange(                                          # one row per changed field
            employee_id=employee_id,
            employee_name=employee_name,
            field=field,
            old_value=old_value,
            new_value=new_value,
            reason=reason,
            source=source,
            actor=actor,                                      # who made it; None -> reader uses source
        )
        for field, old_value, new_value in changes
    ]
    async with async_session() as session:
        session.add_all(rows)                                 # stage every row together
        await session.commit()                                # one commit -> all persist or none do
    return len(rows)                                          # rows actually added


async def list_changes(employee_id: str, limit: int = 2000) -> list[dict]:
    """A person's change history, newest first.

    Args:
        employee_id: Staff Directory item id; compared as a string.
        limit: Most rows to return (newest kept). Raised well above a realistic
            lifetime of hand edits so the oldest rows, the starting-value seeds
            among them, are never cut off the bottom of the newest-first page
            (#455); a person would need thousands of edits to reach it.

    Returns:
        A list of dicts, each {"field", "old_value", "new_value", "reason",
        "source", "actor", "version", "is_start", "changed_at"}, ordered newest
        first (changed_at desc, then id desc for a stable tie-break), with
        changed_at an ISO 8601 UTC string and ``is_start`` true on the starting
        rows seeded from the person's oldest imported SharePoint version.
    """
    employee_id = str(employee_id)                            # match how the rows were stored
    async with async_session() as session:
        query = (
            select(StaffChange)
            .where(StaffChange.employee_id == employee_id)    # this person's rows only
            .order_by(StaffChange.changed_at.desc(), StaffChange.id.desc())  # newest first, id breaks ties
            .limit(limit)                                     # cap the page size
        )
        rows = (await session.scalars(query)).all()
    starts = _start_versions(rows)                            # oldest imported version per person
    return [
        {
            "field": r.field,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "reason": r.reason,
            "source": r.source,
            "actor": r.actor,                                 # who made it; None -> reader uses source
            "version": r.version,                             # SharePoint version label, or None
            "is_start": _is_start_row(r, starts),             # a starting-value seed row (#448)
            "changed_at": _to_utc_iso(r.changed_at),          # stored timestamp -> ISO UTC string
        }
        for r in rows
    ]


async def list_changes_for(employee_ids: list[str], limit: int = 5000) -> list[dict]:
    """Every change for a set of people, newest first, for the staff export.

    One query across all the given employees (the export's change-history sheet
    needs them together), rather than a read per person.

    Args:
        employee_ids: Staff Directory item ids; compared as strings.
        limit: Most rows to return in total (newest kept).

    Returns:
        A list of dicts, each {"employee_id", "employee_name", "field",
        "old_value", "new_value", "reason", "source", "actor", "version",
        "is_start", "changed_at"}, ordered newest first, with changed_at an ISO
        8601 UTC string. Empty when the id list is empty.
    """
    ids = [str(i) for i in employee_ids]                      # match how rows were stored
    if not ids:                                               # nothing to look up
        return []
    async with async_session() as session:
        query = (
            select(StaffChange)
            .where(StaffChange.employee_id.in_(ids))          # any of the listed people
            .order_by(StaffChange.changed_at.desc(), StaffChange.id.desc())  # newest first
            .limit(limit)                                     # cap the total
        )
        rows = (await session.scalars(query)).all()
    if len(rows) >= limit:                                    # the page filled: older rows were left off
        logger.warning(
            "Staff change export hit the %d-row limit for %d people; older rows are not in this export.",
            limit, len(ids),
        )
    starts = _start_versions(rows)                            # oldest imported version per person
    return [
        {
            "employee_id": r.employee_id,
            "employee_name": r.employee_name,                 # name as it was at the time
            "field": r.field,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "reason": r.reason,
            "source": r.source,
            "actor": r.actor,                                 # who made it; None -> reader uses source
            "version": r.version,                             # SharePoint version label, or None
            "is_start": _is_start_row(r, starts),             # a starting-value seed row (#448)
            "changed_at": _to_utc_iso(r.changed_at),
        }
        for r in rows
    ]
