"""Detect direct SharePoint Staff Directory edits after the switch (#268, #261).

Once staff live in Postgres, Postgres is the only source of truth for staff. The
app keeps writing its own changes back to the SharePoint list (the write-back
drain) so the Bulletin Board, the Microsoft Form and the Power Automate flows stay
current, but edits people make directly in SharePoint are no longer pulled back
into Postgres for any column. Letting SharePoint edits win would let a stale row
(or a write-back that has not drained yet) silently reset a balance or any other
field with no ledger or history behind it.

So this step is a detector, not a writer. For each SharePoint record it:

- leaves a person alone when the app still has an unsettled write-back for them
  (pending, failed or in flight): SharePoint is simply behind, not a discrepancy;
- files one legacy finding for HR when the SharePoint row differs from the
  Postgres row in any column the app models (balances included), recording the
  differing columns with both values as evidence, and changes nothing in Postgres;
- files one legacy finding when a SharePoint staff item has no matching Postgres
  row at all (added directly in SharePoint), without importing it.

Findings go through the shared ``legacy_findings.record_finding``, whose
fingerprint (``case:subject:kind:subject_id``) keeps a rerun from filing the same
person's finding twice; a different kind of discrepancy (a different case) files
on its own. Each record is handled inside its own savepoint, so one bad item never
fails the whole tick, and the step writes nothing to the staff tables, so it needs
no row lock. The whole step is gated off in rehearsal mode, like the write-back.
"""
import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.staff import Staff, StaffWriteback
from app.repositories.postgres.staff import diff_logged_fields, staff_query, to_sp_fields
from app.services.legacy_findings import record_finding
from app.services.staff_writeback import FAILED, PENDING, SENDING

logger = logging.getLogger(__name__)

# The finding filed when an existing Postgres person's SharePoint row differs; a
# SharePoint edit after the switch to columns the app now owns. And the finding
# for a SharePoint item that has no Postgres row. Both are values the
# legacy_findings CHECK allows (request_vocab.FINDING_CASES).
CASE_SHAREPOINT_EDIT_AFTER_SWITCH = "sharepoint_edit_after_switch"
CASE_SHAREPOINT_NEW_AFTER_SWITCH = "sharepoint_new_after_switch"

# Write-back statuses that mean "an app change for this person has not landed in
# SharePoint yet", so the detector leaves the person alone (SharePoint is behind).
_UNSETTLED_WRITEBACK_STATUSES = (PENDING, FAILED, SENDING)


def _writes_allowed() -> bool:
    """Whether the detector may read the live SharePoint list (off in rehearsal).

    A missing rehearsal module counts as allowed, so this works on its own until
    that work is joined.

    Returns:
        True when the detector may read the SharePoint list.
    """
    try:
        from app.services.rehearsal import sharepoint_writes_allowed
    except ImportError:
        return True
    return sharepoint_writes_allowed()


def _empty_counts() -> dict:
    """The zeroed counts dict the detector returns, one place so both paths agree.

    Returns:
        Every count the detector reports, at zero.
    """
    return {
        "seen": 0, "unchanged": 0, "diff_findings": 0, "new_findings": 0,
        "skipped_pending": 0, "skipped": 0, "failed": 0,
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


async def _flag_difference(
    session: AsyncSession, row: Staff, sp_id: str, fields: dict, run_id
) -> bool:
    """File one finding when a SharePoint row differs from the app-owned Postgres row.

    Compares every column the app models (``diff_logged_fields`` is the shared
    notion of a logged column: text, numbers including the five balances, dates and
    the extension; numbers are compared as numbers). When any differ, one
    ``sharepoint_edit_after_switch`` finding is filed for HR with the differing
    columns and both values as evidence; Postgres is never changed. The fingerprint
    carries a digest of the differences, so a rerun never files the same difference
    twice, while a later, different edit files again.

    Args:
        session: The open session; a finding is added, not committed.
        row: The matched staff row, with its relationships loaded.
        sp_id: The SharePoint item id, kept in the evidence.
        fields: The incoming SharePoint fields.
        run_id: The sync run id, stamped on the finding.

    Returns:
        True when a new finding was filed (False when identical, or a deduped rerun).
    """
    stored = to_sp_fields(row)                             # the app-owned record, SharePoint-shaped
    diffs = diff_logged_fields(stored, fields)             # [(column, postgres value, sharepoint value)]
    if not diffs:                                          # identical: nothing to flag
        return False
    evidence = {                                           # the differing columns, both sides
        "sp_item_id": sp_id,
        "columns": {column: {"postgres": old, "sharepoint": new} for column, old, new in diffs},
    }
    # A digest of exactly what differs: the same difference never files twice,
    # but a new one (even after HR resolved the last) files a new finding.
    digest = hashlib.sha256(
        json.dumps(evidence["columns"], sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    filed = await record_finding(
        session,
        CASE_SHAREPOINT_EDIT_AFTER_SWITCH,
        "staff",
        None,                                              # staff findings carry no request kind
        row.sp_item_id,                                    # the stable staff identity
        "The SharePoint record differs from the app-owned record in Postgres; HR should reconcile it.",
        evidence,
        run_id,
        variant=digest,                                    # one finding per distinct difference
    )
    return filed is not None


async def _flag_new_item(session: AsyncSession, sp_id: str, fields: dict, run_id) -> bool:
    """File one finding for a SharePoint staff item that has no Postgres row.

    The item is never imported (Postgres is the source of truth); HR decides what
    to do with a record someone added directly in SharePoint. The fingerprint keeps
    the same item from being flagged twice.

    Args:
        session: The open session; a finding is added, not committed.
        sp_id: The SharePoint item id (the finding's subject).
        fields: The item's SharePoint fields, for the evidence.
        run_id: The sync run id, stamped on the finding.

    Returns:
        True when a new finding was filed (False when it was a deduped rerun).
    """
    evidence = {                                           # enough for HR to recognise the person
        "sp_item_id": sp_id,
        "name": (fields.get("Title") or "").strip(),
        "email": (fields.get("EmailAddress") or "").strip(),
    }
    filed = await record_finding(
        session,
        CASE_SHAREPOINT_NEW_AFTER_SWITCH,
        "staff",
        None,
        sp_id,                                             # the SharePoint item id itself
        "A staff item exists in SharePoint with no matching Postgres record; HR should reconcile it.",
        evidence,
        run_id,
    )
    return filed is not None


async def _detect_one(
    session: AsyncSession, sp_id: str, fields: dict, counts: dict, run_id
) -> None:
    """Classify one SharePoint record and file a finding when it is a discrepancy.

    Matches the SharePoint id against both keys (an imported person's
    ``sp_item_id`` or a Postgres-native person's ``sp_remote_item_id``). No match
    means the item has no Postgres row, so it is flagged as new. A matched person
    with an unsettled write-back is left alone; otherwise its SharePoint row is
    compared and a difference is flagged. Nothing in Postgres is changed either way.

    Args:
        session: The open session (this call runs inside a per-item savepoint).
        sp_id: The SharePoint item id.
        fields: The incoming SharePoint fields.
        counts: The run counts.
        run_id: The sync run id, for a filed finding.
    """
    row = (await session.execute(
        staff_query().where((Staff.sp_item_id == sp_id) | (Staff.sp_remote_item_id == sp_id))
    )).scalars().first()

    if row is None:                                        # in SharePoint, not in Postgres
        if await _flag_new_item(session, sp_id, fields, run_id):
            counts["new_findings"] += 1
        return

    # An app change still on its way to SharePoint means SharePoint is behind, so
    # leave this person alone: the gap is the app's own unsent edit, not an edit
    # made in SharePoint. Pending, failed and in-flight write-backs all count.
    if await _has_unsettled_writeback(session, row.sp_item_id):
        counts["skipped_pending"] += 1
        return

    if await _flag_difference(session, row, sp_id, fields, run_id):
        counts["diff_findings"] += 1
    else:
        counts["unchanged"] += 1                           # in Postgres and identical


async def staff_pull_step(session: AsyncSession, sync_run) -> dict:
    """Flag direct SharePoint staff edits for HR, changing nothing in Postgres. No commit.

    For each SharePoint record: a record with no Postgres row is flagged as new
    (never imported); a person with an unsettled write-back is left alone; and a
    person whose SharePoint row differs from Postgres in any app-modelled column is
    flagged with both values as evidence. Each record is handled inside its own
    savepoint, so one bad record never aborts the run. The caller commits the
    findings (the step itself never writes a staff row).

    Args:
        session: The caller's open session; the caller commits it.
        sync_run: The ``SyncRun`` row (or None in a test); its id is stamped on
            any finding filed.

    Returns:
        ``{"seen", "unchanged", "diff_findings", "new_findings", "skipped_pending",
        "skipped", "failed"}``; ``skipped`` is -1 when rehearsal mode turned the
        detector off, ``skipped_pending`` counts people left alone for an unsettled
        write-back.
    """
    if not _writes_allowed():                              # rehearsal: do not touch the live list
        logger.info("Staff pull skipped: SharePoint access is off (rehearsal mode)")
        counts = _empty_counts()
        counts["skipped"] = -1
        return counts

    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    run_id = getattr(sync_run, "id", None)
    counts = _empty_counts()

    for item in items:
        sp_id = str(item.get("id"))
        fields = dict(item.get("fields", {}))
        counts["seen"] += 1
        try:
            async with session.begin_nested():             # one bad item cannot poison the run
                await _detect_one(session, sp_id, fields, counts, run_id)
        except Exception as exc:                            # record it, keep going
            counts["failed"] += 1
            logger.exception("Staff pull failed for #%s: %s", sp_id, exc)
            continue

    return counts
