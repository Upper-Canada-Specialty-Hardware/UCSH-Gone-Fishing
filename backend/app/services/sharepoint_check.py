"""Watch the SharePoint request lists after the switch to Postgres (#336).

Once requests live in Postgres the Microsoft Form is retired (turned off at the
switch, no fallback), but a person could still edit or delete an item by hand,
and a stray item could still land in a list until the Form and its Power Automate
flow are switched off. This sweep catches all of that. It runs at 30 minutes,
then 1 hour, then daily after the flip (scheduled from the flip time in
sync_tasks), reads each list, and decides what each item is:

- a new item (Form submission or not) is NEVER imported as a request: it is
  flagged ``sharepoint_new_after_switch`` with its id, list, submitter and dates,
  for an admin to handle by hand on the Legacy data screen;
- an edit to an item already known is never applied, only flagged
  ``sharepoint_edit_after_switch``;
- an item that has gone from SharePoint is never applied, only flagged
  ``sharepoint_delete_after_switch``.

Items are matched to Postgres by ``sp_item_id`` only, so two different real
requests that happen to read alike are never taken for one. Each
``(kind, sp_item_id, sp_version)`` is recorded once in ``sharepoint_seen``, so a
rerun seeing the same version again does nothing.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.models.sync_run import SyncRun
from app.services.request_import import (
    KIND_LIST_SETTING,
    item_version,
    record_seen_version,
    record_finding,
)

logger = logging.getLogger(__name__)

# The marker version used for a delete, so a gone item is flagged only once.
DELETED_VERSION = "__deleted__"

# The SharePoint columns the request page writes a start/end date into, per kind,
# so the finding evidence shows the dates whichever list the item is on.
_DATE_COLUMNS = {
    "leave": ("StartDate", "EndDate"),
    "overtime": ("StartDate", None),            # overtime is a single worked date
    "carryover_payout": (None, None),           # carry-over/payout carries no dates
}


def _new_item_evidence(kind: str, fields: dict, version: str) -> dict:
    """The evidence for a "new SharePoint item after the switch" finding.

    Args:
        kind: The request kind (which picks the date columns).
        fields: The item's SharePoint fields.
        version: The item's version string.

    Returns:
        The item's list, submitter and dates, so HR can trace and re-key it.
    """
    start_col, end_col = _DATE_COLUMNS.get(kind, (None, None))
    return {
        "list_id": getattr(settings, KIND_LIST_SETTING[kind]),   # which SharePoint list it landed in
        "request_source": fields.get("RequestSource"),           # blank means it came from the Form
        "submitter_email": fields.get("SubmitterEmail"),         # who submitted, when the item carries it
        "start_date": fields.get(start_col) if start_col else None,
        "end_date": fields.get(end_col) if end_col else None,
        "sp_version": version,                                   # the version flagged
    }


def _version(sp_item: dict) -> str:
    """The version string for a SharePoint item (shared with the importer)."""
    return item_version(sp_item)


async def _seen_versions(session: AsyncSession, kind: str, sp_item_id: str) -> set[str]:
    """Every version of one item the sweep has already acted on.

    Args:
        session: An open session.
        kind: The request kind.
        sp_item_id: The SharePoint item id.

    Returns:
        The set of recorded ``sp_version`` values.
    """
    rows = (await session.execute(
        select(SharepointSeen.sp_version).where(
            SharepointSeen.kind == kind, SharepointSeen.sp_item_id == str(sp_item_id)
        )
    )).all()
    return {r[0] for r in rows}


async def _record_seen(
    session: AsyncSession, kind: str, sp_item_id: str, sp_version: str, outcome: str, run_id: int | None
) -> None:
    """Record one item version and its outcome, at most once per version.

    Args:
        session: An open session; not committed here.
        kind: The request kind.
        sp_item_id: The SharePoint item id.
        sp_version: The version acted on.
        outcome: One of request_vocab.SEEN_OUTCOMES.
        run_id: The sync_runs row this belongs to.
    """
    await record_seen_version(session, kind, sp_item_id, sp_version, outcome, run_id)


async def _known(session: AsyncSession, kind: str, sp_item_id: str) -> bool:
    """Whether a Postgres request already exists for a SharePoint item.

    Args:
        session: An open session.
        kind: The request kind.
        sp_item_id: The SharePoint item id.

    Returns:
        True when a RequestRecord carries that sp_item_id.
    """
    found = (await session.execute(
        select(RequestRecord.id).where(
            RequestRecord.kind == kind, RequestRecord.sp_item_id == str(sp_item_id)
        )
    )).first()
    return found is not None


async def _check_item(session, kind, sp_item, run_id, counts) -> None:
    """Decide and act on one SharePoint item, recording its outcome once.

    Since the switch only flags items (it never imports them), it needs no staff
    index, so none is read for a sweep.

    Args:
        session: An open session; the caller commits.
        kind: The request kind.
        sp_item: The SharePoint item.
        run_id: The sync_runs row for this sweep.
        counts: The running counts dict, mutated in place.
    """
    sp_item_id = str(sp_item.get("id"))
    version = _version(sp_item)
    seen = await _seen_versions(session, kind, sp_item_id)
    if version in seen:
        return                                                 # this exact version was already handled

    known = await _known(session, kind, sp_item_id)
    fields = sp_item.get("fields", {})

    if not known:
        # The Microsoft Form is retired at the switch (no fallback). A new item in
        # a SharePoint request list after the switch is NEVER imported as a request
        # (no app number, no manager notification): the request page is the only
        # intake now. It is recorded as one legacy finding, Form submission or not,
        # so an admin sees it on the Legacy data screen and handles it by hand
        # (#336 review, revised scope). The evidence carries the item's id, list,
        # submitter and dates so HR can trace and re-key it.
        await record_finding(
            session, "sharepoint_new_after_switch", "request", kind, sp_item_id,
            "A new item appeared in a SharePoint request list after the switch; it was not imported.",
            _new_item_evidence(kind, fields, version), run_id,
        )
        outcome, counter = "flagged_new", "flagged_new"
    elif not seen:
        # Known but no version on record: imported before versions were kept.
        # Take this one as the baseline; a later version is an edit.
        outcome, counter = "unchanged", "unchanged"
    else:
        # Known and seen before at another version: an edit. Never applied.
        await record_finding(
            session, "sharepoint_edit_after_switch", "request", kind, sp_item_id,
            "A request was edited in SharePoint after the switch; the edit was not applied.",
            {"sp_version": version}, run_id,
        )
        outcome, counter = "flagged_edit", "flagged_edit"

    await _record_seen(session, kind, sp_item_id, version, outcome, run_id)
    counts[counter] = counts.get(counter, 0) + 1


async def _check_deletes(session, kind, current_ids, run_id, counts) -> None:
    """Flag imported requests whose SharePoint item has gone. Never applied.

    Args:
        session: An open session; the caller commits.
        kind: The request kind.
        current_ids: The sp_item_ids present in SharePoint now.
        run_id: The sync_runs row for this sweep.
        counts: The running counts dict, mutated in place.
    """
    imported = (await session.execute(
        select(RequestRecord.sp_item_id).where(
            RequestRecord.kind == kind, RequestRecord.sp_item_id.is_not(None)
        )
    )).all()
    for (sp_item_id,) in imported:
        if sp_item_id in current_ids:
            continue                                           # still there
        if DELETED_VERSION in await _seen_versions(session, kind, sp_item_id):
            continue                                           # already flagged as deleted
        await record_finding(
            session, "sharepoint_delete_after_switch", "request", kind, sp_item_id,
            "A request was deleted in SharePoint after the switch; the delete was not applied.",
            {}, run_id,
        )
        await _record_seen(session, kind, sp_item_id, DELETED_VERSION, "flagged_delete", run_id)
        counts["flagged_delete"] = counts.get("flagged_delete", 0) + 1


def _kind_for_list(list_id: str) -> str | None:
    """The request kind a SharePoint list id belongs to, or None.

    Args:
        list_id: A SharePoint list id.

    Returns:
        "leave", "overtime" or "carryover_payout", or None for any other list.
    """
    for kind, setting_name in KIND_LIST_SETTING.items():
        if getattr(settings, setting_name) == list_id:
            return kind
    return None


async def run_sharepoint_check_for_item(list_id: str, item_id) -> dict:
    """Run the post-switch check for one SharePoint item at once (the webhook path).

    Once requests live in Postgres the request-list webhook is otherwise ignored,
    but a person (or the retired Form, until it is turned off) can still write to
    the list. This handles the one changed item now, through the same
    ``_check_item`` and ``sharepoint_seen`` logic, so a new item is flagged for an
    admin without waiting for the scheduled sweep and a rerun does nothing. A
    deleted item is left to the scheduled sweep, which flags deletes.

    Args:
        list_id: The request list the change came from.
        item_id: The changed item's SharePoint id.

    Returns:
        The per-outcome counts for this one item.
    """
    kind = _kind_for_list(list_id)
    if kind is None:
        return {}                                              # not a request list
    sp_item = await sp_client.get_list_item_or_none(list_id, item_id)
    if sp_item is None:
        return {}                                              # gone: the scheduled sweep flags deletes
    counts: dict = {}
    async with async_session() as session:
        # Its own job name, so the scheduled-sweep counter never mistakes a webhook
        # check for a full sweep and postpones the 30 min / 1 h / daily sweeps (#336 fix).
        run = SyncRun(job="sharepoint_check_item")
        session.add(run)
        await session.flush()
        try:
            async with session.begin_nested():                 # isolate this one item
                await _check_item(session, kind, sp_item, run.id, counts)
        except Exception:
            # The savepoint rolled this item back, so the scheduled sweep retries
            # it; record its id so the failure is visible, never silently dropped.
            counts["errors"] = counts.get("errors", 0) + 1
            counts.setdefault("error_ids", []).append(str(item_id))
            logger.exception("SharePoint check (one item): failed on %s item %s", kind, item_id)
        run.counts = counts
        run.finished_at = utcnow()
        await session.commit()
    _wake_outbox()                                             # deliver anything a prior step queued
    return counts


def _wake_outbox() -> None:
    """Nudge the outbox worker after a check may have queued messages (best-effort)."""
    try:
        from app.tasks.outbox_worker import kick_outbox
        kick_outbox()
    except Exception:                                          # a missing worker (tests) is harmless
        pass


async def run_sharepoint_check() -> dict:
    """Sweep the three SharePoint request lists once, recording a SyncRun.

    Returns:
        The per-outcome counts, e.g.
        ``{"imported_form": 1, "flagged_edit": 2, "flagged_delete": 1, ...}``.
    """
    async with async_session() as session:
        run = SyncRun(job="sharepoint_check")
        session.add(run)
        await session.flush()
        counts: dict = {}
        try:
            for kind, setting_name in KIND_LIST_SETTING.items():
                list_id = getattr(settings, setting_name)
                items = await sp_client.get_list_items(list_id)
                current_ids = {str(item.get("id")) for item in items}
                for sp_item in items:
                    try:
                        async with session.begin_nested():     # isolate one item from the rest
                            await _check_item(session, kind, sp_item, run.id, counts)
                    except Exception:
                        # Rolled back and not recorded as seen, so the next sweep
                        # retries it; its id is kept so the failure stays visible.
                        counts["errors"] = counts.get("errors", 0) + 1
                        counts.setdefault("error_ids", []).append(str(sp_item.get("id")))
                        logger.exception("SharePoint check: failed on %s item %s", kind, sp_item.get("id"))
                await _check_deletes(session, kind, current_ids, run.id, counts)
            run.counts = counts
            run.finished_at = utcnow()
            await session.commit()
        except Exception as e:
            run.error = str(e)
            run.finished_at = utcnow()
            await session.commit()
            logger.exception("SharePoint check failed")
            counts = {"error": str(e)}
    _wake_outbox()                                              # deliver any notifications the sweep queued
    return counts
