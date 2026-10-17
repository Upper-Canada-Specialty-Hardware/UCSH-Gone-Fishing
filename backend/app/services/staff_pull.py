"""Pull edits made directly in the SharePoint Staff Directory into Postgres (#268).

People keep editing the SharePoint list during and after the move: HR fixes a
record there, a flow updates a field, someone corrects a balance. Most of those
edits have to reach Postgres, so this reads the list and, for each person, keeps
whichever side's edit is newer by modified time. When the SharePoint edit wins,
the typed row is updated and each changed field is written to ``staff_changes``
with source ``"sharepoint"`` so the history shows where the change came from.

Two rules keep the app's own writes safe once staff live in Postgres (#261
finding 2):

- The five balance pots are app-owned. The pull never copies them from
  SharePoint; a SharePoint balance that differs from the Postgres one becomes a
  reconciliation finding for HR instead, recorded once with both values, and
  Postgres is left unchanged.
- A person with an app change still on its way to SharePoint (a write-back that
  is pending, failed or in flight) is left entirely alone: SharePoint is simply
  behind, so the pull must not clobber the app's newer edit, and must not read a
  stale balance as a discrepancy.

Each person is handled under a row lock and re-read after the lock, so a commit
that lands between the SharePoint list read and this update is not lost, and each
person is handled inside its own savepoint, so one bad item never fails the whole
tick. A pulled edit is never queued for write-back, so a change that originated in
SharePoint is not echoed back. The pull is gated off in rehearsal mode, the same
as the write-back, so a rehearsal never touches the live list.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.models.staff import Staff, StaffWriteback
from app.models.staff_change import StaffChange
from app.repositories.postgres.staff import (
    _norm_email,
    _parse_dt,
    apply_sp_fields,
    diff_logged_fields,
    staff_query,
    to_sp_fields,
)
from app.services.legacy_findings import record_finding
from app.services.staff_writeback import FAILED, PENDING, SENDING

logger = logging.getLogger(__name__)

# The five balance columns the app owns once staff live in Postgres (the pots in
# repositories/postgres/staff_balances mapped to their Staff Directory columns).
# The pull never writes these; a differing SharePoint value is a finding for HR.
_BALANCE_COLUMNS = (
    "CurrentSickDayBalance",
    "CurrentOvertimeBalance",
    "CarryOver",
    "CurrentVacationBalance",
    "Payout",
)

# The finding filed when a SharePoint balance differs from the app-owned one; a
# SharePoint edit after the switch to a column the app now owns. One of the
# values the legacy_findings CHECK allows (request_vocab.FINDING_CASES).
CASE_SHAREPOINT_EDIT_AFTER_SWITCH = "sharepoint_edit_after_switch"

# Write-back statuses that mean "an app change for this person has not landed in
# SharePoint yet", so the pull must leave the person alone.
_UNSETTLED_WRITEBACK_STATUSES = (PENDING, FAILED, SENDING)


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


def _num(value) -> float:
    """A balance value as a float, blank counting as zero, for a safe compare.

    Args:
        value: A balance column value (a number, a number string, or blank).

    Returns:
        The value as a float, or 0.0 when blank or unreadable, so a blank pot and
        a zero pot never read as a difference.
    """
    try:
        return float(value or 0)                           # None/""/0 -> 0.0
    except (TypeError, ValueError):
        return 0.0


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


def _empty_counts() -> dict:
    """The zeroed counts dict the pull returns, one place so both paths agree.

    Returns:
        Every count the pull reports, at zero.
    """
    return {
        "seen": 0, "applied": 0, "imported": 0, "skipped_older": 0,
        "changes_recorded": 0, "skipped": 0, "skipped_pending": 0,
        "balance_findings": 0, "email_conflicts": 0, "failed": 0,
    }


async def _has_unsettled_writeback(session: AsyncSession, sp_item_id: str) -> bool:
    """Whether a staff write-back for this person is pending, failed or in flight.

    Keyed on the staff row's own id (the minted id for a Postgres-native person,
    the SharePoint id for an imported one), which is what the write-back stores.

    Args:
        session: The open session.
        sp_item_id: The staff row's ``sp_item_id``.

    Returns:
        True when an app change for this person has not yet reached SharePoint.
    """
    found = (await session.execute(
        select(StaffWriteback.id)
        .where(
            StaffWriteback.sp_item_id == str(sp_item_id),
            StaffWriteback.status.in_(_UNSETTLED_WRITEBACK_STATUSES),
        )
        .limit(1)
    )).first()
    return found is not None


async def _email_taken(session: AsyncSession, norm_email: str, exclude_sp_id: str) -> bool:
    """Whether another staff row already holds this normalised email.

    Args:
        session: The open session.
        norm_email: The normalised (trimmed, lower-case) email.
        exclude_sp_id: The sp id being imported, so a row never clashes with itself.

    Returns:
        True when a different staff row already has that email.
    """
    found = (await session.execute(
        select(Staff.id)
        .where(Staff.email == norm_email, Staff.sp_item_id != str(exclude_sp_id))
        .limit(1)
    )).first()
    return found is not None


async def _import_new(
    session: AsyncSession, sp_id: str, fields: dict, incoming_modified, seen_emails: dict, counts: dict
) -> None:
    """Import a staff item that appeared in SharePoint after the first import.

    Applies the same email de-dupe as the staff importer: a duplicate email is
    stored as blank rather than violating the unique email index, so one clashing
    item never fails the whole pull. Balances come in as-is on a first import
    (there is nothing app-owned to protect yet).

    Args:
        session: The open session.
        sp_id: The SharePoint item id.
        fields: The incoming SharePoint fields (mutated to blank a duplicate email).
        incoming_modified: The item's modified time, or None.
        seen_emails: Normalised email -> first sp id seen in this run, for in-run
            de-dupe between two new items that claim the same address.
        counts: The run counts, bumped for the import and any email conflict.
    """
    norm = _norm_email(fields.get("EmailAddress"))
    # Blank the email when another item this run already claimed it, or a row in
    # Postgres already holds it, so the unique index never fails the whole tick.
    if norm and (seen_emails.get(norm, sp_id) != sp_id or await _email_taken(session, norm, sp_id)):
        fields["EmailAddress"] = ""                        # -> NULL, no collision
        counts["email_conflicts"] += 1
        logger.warning("Staff pull: email on #%s duplicates another record; imported without it", sp_id)
    elif norm:
        seen_emails[norm] = sp_id                          # first item to claim this address
    row = Staff(sp_item_id=sp_id)
    session.add(row)
    await apply_sp_fields(session, row, fields)
    row.sp_modified = incoming_modified or utcnow()
    counts["imported"] += 1


async def _reconcile_balances(
    session: AsyncSession, row: Staff, sp_id: str, fields: dict, counts: dict, run_id
) -> None:
    """File one HR finding when a SharePoint balance differs from the app-owned one.

    The five balance pots are app-owned once staff live in Postgres, so the pull
    never copies them. When SharePoint shows a different value for any of them (and
    no write-back is pending, which the caller has already checked), the gap is
    recorded once as a ``sharepoint_edit_after_switch`` finding for HR, with both
    values as evidence; Postgres is left unchanged. The fingerprint keeps a rerun
    from filing the same finding twice.

    Args:
        session: The open session; a finding is added, not committed.
        row: The locked staff row, holding the app-owned balances.
        sp_id: The SharePoint item id, kept in the evidence.
        fields: The incoming SharePoint fields.
        counts: The run counts, bumped when a new finding is filed.
        run_id: The sync run id, stamped on the finding.
    """
    stored = to_sp_fields(row)                             # the app's balances, SharePoint-shaped
    differences: dict = {}
    for column in _BALANCE_COLUMNS:
        if column not in fields:                           # a partial payload leaves a pot alone
            continue
        sp_value = _num(fields.get(column))                # SharePoint's value, blank as zero
        pg_value = _num(stored.get(column))                # the app-owned value, blank as zero
        if sp_value != pg_value:                           # a real move, not 2 vs "2.0" or blank vs 0
            differences[column] = {"sharepoint": sp_value, "postgres": pg_value}
    if not differences:                                    # balances agree: nothing to reconcile
        return
    filed = await record_finding(
        session,
        CASE_SHAREPOINT_EDIT_AFTER_SWITCH,
        "staff",
        None,                                              # staff findings carry no request kind
        row.sp_item_id,                                    # the stable staff identity
        "A SharePoint balance differs from the app-owned balance in Postgres; HR should reconcile it.",
        {"sp_item_id": sp_id, "balances": differences},    # both values, per differing pot
        run_id,
    )
    if filed is not None:                                  # a fresh finding, not a deduped rerun
        counts["balance_findings"] += 1


async def _pull_one(
    session: AsyncSession, sp_id: str, fields: dict, incoming_modified, seen_emails: dict,
    counts: dict, run_id,
) -> None:
    """Apply one SharePoint record to Postgres, with the balance and write-back rules.

    Imports a person not yet in Postgres (with the email de-dupe). For an existing
    person: takes a row lock and re-reads the row (so a concurrent commit is not
    lost); leaves the person alone when an app change is still unsettled; files a
    balance finding instead of copying a differing balance; and applies the newer
    non-balance fields, logging each.

    Args:
        session: The open session (this call runs inside a per-item savepoint).
        sp_id: The SharePoint item id.
        fields: The incoming SharePoint fields.
        incoming_modified: The item's modified time, or None.
        seen_emails: Normalised email -> first sp id, for the import de-dupe.
        counts: The run counts.
        run_id: The sync run id, for a filed finding.
    """
    # Match on the SharePoint id against either key: an imported person stores it
    # as sp_item_id, a Postgres-native person as sp_remote_item_id once the create
    # write-back ran. The match doubles as the row lock and re-read: on Postgres it
    # is taken FOR UPDATE and repopulated, so the state read here is the committed
    # state at lock time, not a value that a commit since the SharePoint list read
    # has already moved. On SQLite (one writer anyway) it is a plain re-read.
    statement = staff_query().where(
        (Staff.sp_item_id == sp_id) | (Staff.sp_remote_item_id == sp_id)
    )
    if session.bind.dialect.name == "postgresql":          # real row lock only where it exists
        statement = statement.with_for_update(of=Staff)
    statement = statement.execution_options(populate_existing=True)   # re-read under the lock
    row = (await session.execute(statement)).scalars().first()

    if row is None:                                        # appeared in SharePoint after the import
        await _import_new(session, sp_id, fields, incoming_modified, seen_emails, counts)
        return

    # An app change still on its way to SharePoint means SharePoint is behind, so
    # leave this person alone: do not clobber the app's newer edit, and do not read
    # a stale balance as a discrepancy. Pending, failed and in-flight all count.
    if await _has_unsettled_writeback(session, row.sp_item_id):
        counts["skipped_pending"] += 1
        return

    # Newer wins: an older or equal SharePoint edit changes nothing, and its stale
    # balance must not be reconciled against the app's current one.
    stored_modified = _aware(row.sp_modified)              # SQLite returns it naive
    if incoming_modified is not None and stored_modified is not None and incoming_modified <= stored_modified:
        counts["skipped_older"] += 1
        return

    # SharePoint's edit is newer. Balances are app-owned, so a differing balance is
    # a finding for HR, never an overwrite; Postgres is left unchanged either way.
    await _reconcile_balances(session, row, sp_id, fields, counts, run_id)

    # Apply only the non-balance fields, newer-wins, and log each change. Dropping
    # the balance columns here is what makes the pull never write a pot.
    non_balance = {column: value for column, value in fields.items() if column not in _BALANCE_COLUMNS}
    before_fields = to_sp_fields(row)                      # for the change log, before applying
    name_now = (fields.get("Title") or before_fields.get("Title") or "").strip()
    changes = diff_logged_fields(before_fields, non_balance)
    await apply_sp_fields(session, row, non_balance)
    row.sp_modified = incoming_modified or utcnow()
    counts["applied"] += 1
    for column, old, new in changes:                       # one history row per changed field
        session.add(StaffChange(
            employee_id=row.sp_item_id,
            employee_name=name_now,
            field=column,
            old_value=old,
            new_value=new,
            reason="Pulled from the SharePoint Staff Directory.",
            source="sharepoint",
        ))
        counts["changes_recorded"] += 1


async def staff_pull_step(session: AsyncSession, sync_run) -> dict:
    """Apply newer SharePoint edits to the staff table, logging each change. No commit.

    For each SharePoint record: a person not yet in Postgres is imported (with the
    same email de-dupe as the importer); an existing person is handled under a row
    lock and the balance / write-back rules above. Balances are never written by
    the pull; a differing SharePoint balance is filed as a finding for HR. Each
    person is processed inside its own savepoint, so one bad record never aborts
    the run. No write-back is queued, so there is no echo. The caller commits.

    Args:
        session: The caller's open session; the caller commits it.
        sync_run: The ``SyncRun`` row (or None in a test); its id is stamped on
            any finding filed.

    Returns:
        ``{"seen", "applied", "imported", "skipped_older", "changes_recorded",
        "skipped", "skipped_pending", "balance_findings", "email_conflicts",
        "failed"}``; ``skipped`` is -1 when rehearsal mode turned the pull off,
        ``skipped_pending`` counts people left alone for an unsettled write-back.
    """
    if not _writes_allowed():                              # rehearsal: do not touch the live list
        logger.info("Staff pull skipped: SharePoint access is off (rehearsal mode)")
        counts = _empty_counts()
        counts["skipped"] = -1
        return counts

    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    run_id = getattr(sync_run, "id", None)
    counts = _empty_counts()
    seen_emails: dict[str, str] = {}                       # normalised email -> first sp id this run

    for item in items:
        sp_id = str(item.get("id"))
        fields = dict(item.get("fields", {}))
        counts["seen"] += 1
        incoming_modified = _parse_dt(fields.get("Modified")) or _parse_dt(item.get("lastModifiedDateTime"))
        try:
            async with session.begin_nested():             # one bad item cannot poison the run
                await _pull_one(session, sp_id, fields, incoming_modified, seen_emails, counts, run_id)
        except Exception as exc:                            # record it, keep going
            counts["failed"] += 1
            logger.exception("Staff pull failed for #%s: %s", sp_id, exc)
            continue

    return counts
