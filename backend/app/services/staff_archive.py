"""Archive and restore a departed employee from the admin dashboard (#119).

Employees can be added from the admin dashboard but, until now, not taken out:
a departed person stayed in the Staff Directory and kept appearing in balances,
teams, manager assignments and reports. Archiving keeps the person on record
(their past requests and balance history stay) while dropping them out of every
list, team, picker, report and approval route and stopping them making new
requests. An archived person can be restored.

The archive flag lives on the ``staff`` row (``archived_at`` / ``archived_reason``
/ ``archived_by``, Alembic 0024). The filtering that hides archived people from
every listing is in ``staff_store`` / ``repositories/postgres/staff.py`` (the
default ``get_list_items`` leaves them out); single-person lookups by id still
return them, so a past request resolves their name.

Writes go through ``staff_store`` so an open dashboard re-reads (the Postgres
write fires ``pg_notify('staff_changes')``), and the change is recorded in
``staff_changes`` with the reason, like every other admin edit. Staff live in
Postgres after the switch; the SharePoint storage mode is not a target here, so
the routes refuse it (see ``routes/admin_archive.py``).

A person's still-pending requests are left pending on purpose (not auto-rejected)
and listed back to the admin so they know, and an admin can still approve them.
"""
import logging

from app.config import settings
from app.repositories.postgres.staff import is_archived
from app.repositories.request_store import request_store
from app.repositories.staff_store import staff_store
from app.services import staff_changes
from app.services.concurrency import lock_manager
from app.services.employee import get_employee_by_id

logger = logging.getLogger(__name__)

# The admin dashboard has one shared password, no per-user identity, so an
# archive/restore is recorded under the admin channel like every other admin edit.
ARCHIVE_SOURCE = "admin_dashboard"
ARCHIVE_ACTOR = "admin"
# The history field name for an archive/restore, so the person's Activity timeline
# shows it as one readable row carrying the reason.
ARCHIVE_FIELD = "Archived"

# The three request lists, so the confirmation can list a person's pending items.
_REQUEST_LISTS = (
    ("leave", settings.SP_LIST_LEAVE_REQUESTS),
    ("overtime", settings.SP_LIST_OVERTIME_REQUESTS),
    ("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT),
)


class ArchiveError(Exception):
    """An archive or restore could not be done as asked.

    Raised for a blank reason, an already-archived person being archived again,
    or a non-archived person being restored, so the route answers 400/409 rather
    than a generic 500.
    """


async def _pending_requests(employee_id: str) -> list[dict]:
    """A person's still-pending requests, for the archive confirmation.

    Best-effort: archiving must not fail because a request list could not be
    read, so a failed read of one list is logged and skipped rather than raised.
    Pending requests are left pending on purpose (ruling: do not auto-reject), so
    this only lists them.

    Args:
        employee_id: The person's Staff Directory id.

    Returns:
        One ``{"request_type", "item_id"}`` per pending request, newest list
        order; empty when there are none (or none could be read).
    """
    target = str(employee_id)                               # ids are compared as strings everywhere
    pending: list[dict] = []
    for kind, list_id in _REQUEST_LISTS:
        try:
            items = await request_store.get_list_items(list_id)   # all items of the list
        except Exception:  # noqa: BLE001 - a missing list must not block the archive
            logger.exception("Archive: could not read %s requests for %s", kind, employee_id)
            continue
        for item in items:
            fields = item.get("fields", {})
            if str(fields.get("EmployeeID") or "") != target:     # only this person's
                continue
            if str(fields.get("Status") or "").strip().lower() != "pending":   # only pending
                continue
            pending.append({"request_type": kind, "item_id": str(item.get("id"))})
    return pending


async def archive_employee(employee_id: str, reason: str, actor: str = ARCHIVE_ACTOR) -> dict:
    """Archive a departed employee: out of every list, kept on record (#119).

    Under the per-employee lock (so a concurrent balance edit cannot interleave)
    the record is re-read, the archive columns are written through ``staff_store``
    (which notifies open dashboards), and the change is recorded in the staff
    history with the reason. The person's still-pending requests are left pending
    and returned so the admin knows.

    Args:
        employee_id: The person's Staff Directory id.
        reason: Why they are being archived; required and stored on the history.
        actor: Who archived them, recorded as ``archived_by``; the admin channel.

    Returns:
        ``{"employee_id", "name", "pending_requests": [...]}``.

    Raises:
        ArchiveError: Blank reason, or the person is already archived.
        LookupError: No such employee.
    """
    reason = (reason or "").strip()                         # trim surrounding whitespace
    if not reason:                                          # a reason is mandatory, like every admin edit
        raise ArchiveError("A reason is required to archive someone.")

    async with lock_manager.lock(str(employee_id)):        # serialise per-employee writes
        emp = await get_employee_by_id(employee_id)        # single lookup -> archived people included
        if not emp:                                        # gone or never existed
            raise LookupError("Employee not found")
        fields = emp.get("fields", {})
        if is_archived(fields):                            # idempotency guard
            raise ArchiveError("This person is already archived.")
        name = fields.get("Title", "")                     # name at the time, for the history row

        now_iso = _utcnow_iso()                            # the archive moment, stored as ISO
        # Write the archive columns through the store: history_source stays None so
        # the store logs nothing (this service records the one readable row below),
        # and the Postgres write fires pg_notify so open dashboards drop the person.
        await staff_store.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"ArchivedAt": now_iso, "ArchivedReason": reason, "ArchivedBy": actor},
        )
        await _record(employee_id, name, "No", "Yes", reason)   # one history row carrying the reason

    pending = await _pending_requests(employee_id)         # outside the lock: read-only, for the confirmation
    logger.info("Archived staff #%s (%s); %d pending request(s) left pending", employee_id, name, len(pending))
    return {"employee_id": str(employee_id), "name": name, "pending_requests": pending}


async def restore_employee(employee_id: str, actor: str = ARCHIVE_ACTOR) -> dict:
    """Restore an archived employee back into the Staff Directory (#119).

    Clears the three archive columns through ``staff_store`` (so open dashboards
    re-read) and records the restore in the staff history.

    Args:
        employee_id: The person's Staff Directory id.
        actor: Who restored them; recorded only in the history reason.

    Returns:
        ``{"employee_id", "name"}``.

    Raises:
        ArchiveError: The person is not archived.
        LookupError: No such employee.
    """
    async with lock_manager.lock(str(employee_id)):        # serialise per-employee writes
        emp = await get_employee_by_id(employee_id)
        if not emp:
            raise LookupError("Employee not found")
        fields = emp.get("fields", {})
        if not is_archived(fields):                        # nothing to restore
            raise ArchiveError("This person is not archived.")
        name = fields.get("Title", "")

        # Clear every archive column: None un-archives the person and clears the
        # kept reason/actor, so the record reads clean again.
        await staff_store.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"ArchivedAt": None, "ArchivedReason": None, "ArchivedBy": None},
        )
        await _record(employee_id, name, "Yes", "No", "Restored to the Staff Directory.")

    logger.info("Restored staff #%s (%s)", employee_id, name)
    return {"employee_id": str(employee_id), "name": name}


async def list_archived() -> list[dict]:
    """Every archived person, for the staff screen's archived list (#119).

    Reads the directory with archived people included and keeps only those that
    are archived, newest first by when they were archived.

    Returns:
        One row per archived person: ``{"id", "name", "email", "department",
        "location", "archived_at", "archived_reason", "archived_by"}``.
    """
    items = await staff_store.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY, include_archived=True
    )
    archived = []
    for item in items:
        fields = item.get("fields", {})
        if not is_archived(fields):                        # keep only the archived rows
            continue
        archived.append({
            "id": str(item.get("id")),
            "name": fields.get("Title", ""),
            "email": fields.get("EmailAddress", ""),
            "department": fields.get("Department", ""),
            "location": fields.get("Location", ""),
            "archived_at": fields.get("ArchivedAt") or "",
            "archived_reason": fields.get("ArchivedReason", ""),
            "archived_by": fields.get("ArchivedBy", ""),
        })
    # Newest archive first, so the most recent departure is at the top.
    archived.sort(key=lambda r: r["archived_at"], reverse=True)
    return archived


async def _record(employee_id, name: str, before: str, after: str, reason: str) -> None:
    """Record one archive/restore history row, best-effort (never undoes the write).

    Args:
        employee_id: The person's Staff Directory id.
        name: Their name at the time of the change.
        before: The archive state before ("No"/"Yes").
        after: The archive state after ("Yes"/"No").
        reason: Why; stored on the row.
    """
    try:
        await staff_changes.record_changes(
            str(employee_id), name, [(ARCHIVE_FIELD, before, after)], reason, source=ARCHIVE_SOURCE,
        )
    except Exception:  # noqa: BLE001 - history must not undo the archive write
        logger.exception("Could not record archive history for %s", employee_id)


def _utcnow_iso() -> str:
    """The current UTC time as a Graph-style ISO string ("2026-10-19T14:03:22Z").

    Returns:
        The timestamp to the second with a trailing Z, so the store parses it the
        same way it parses a SharePoint timestamp.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
