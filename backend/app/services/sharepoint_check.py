"""Watch the SharePoint request lists after the switch to Postgres (#336).

Once requests live in Postgres, the Microsoft Form and its Power Automate flow
keep writing to the SharePoint lists, and a person could still edit or delete an
item there by hand. This sweep catches all of that. It runs at 30 minutes, then
1 hour, then daily after the flip (scheduled from the flip time in sync_tasks),
reads each list, and decides what each item is:

- a new Microsoft Form submission is imported once and enters the normal flow,
  with a ``submitted`` event on the ``form`` channel;
- any other new item is imported and flagged ``sharepoint_new_after_switch``;
- an edit to an item already known is never applied, only flagged
  ``sharepoint_edit_after_switch``;
- an item that has gone from SharePoint is never applied, only flagged
  ``sharepoint_delete_after_switch``.

Items are matched to Postgres by ``sp_item_id`` only, so two different real
requests that happen to read alike are never taken for one. Each
``(kind, sp_item_id, sp_version)`` is recorded once in ``sharepoint_seen``, so a
rerun seeing the same version again does nothing. A Form item is told apart by
its blank ``RequestSource`` column: the app sets that column for its own request
sources, and the Form never does.
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
    _import_item,
    build_staff_index,
    item_version,
    record_seen_version,
    record_finding,
)

logger = logging.getLogger(__name__)

# The marker version used for a delete, so a gone item is flagged only once.
DELETED_VERSION = "__deleted__"


def _is_form_item(fields: dict) -> bool:
    """Whether a SharePoint item came from the Microsoft Form.

    The app stamps ``RequestSource`` (Request page, API, Admin dashboard, Manager
    dashboard) on everything it creates; the Microsoft Form flow leaves it blank.
    A blank ``RequestSource`` is therefore a Form submission.

    Args:
        fields: The item's SharePoint fields.

    Returns:
        True for a Form submission.
    """
    return not (fields.get("RequestSource") or "").strip()


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


async def _check_item(session, kind, sp_item, index, run_id, counts) -> None:
    """Decide and act on one SharePoint item, recording its outcome once.

    Args:
        session: An open session; the caller commits.
        kind: The request kind.
        sp_item: The SharePoint item.
        index: The loaded staff index.
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
        # Never imported: a genuinely new item in the SharePoint list.
        if _is_form_item(fields):
            await _import_item(session, kind, sp_item, index, run_id,
                               event_type="submitted", event_channel="form")
            # Treat a stray Form request like an app request: assign its manager
            # and queue the notifications, once staff and requests are in Postgres.
            # Shares this session, so the import and the queued messages land
            # together; the submitted key means a rerun queues nothing again (#326).
            from app.services.request_submission import submission_active, submit
            if await submission_active():
                await submit(kind, int(sp_item_id), channel="form", notify=True, session=session)
            outcome, counter = "imported", "imported_form"
        else:
            await _import_item(session, kind, sp_item, index, run_id,
                               event_type="imported", event_channel="sharepoint_check")
            await record_finding(
                session, "sharepoint_new_after_switch", "request", kind, sp_item_id,
                "A non-Form item appeared in SharePoint after the switch to Postgres.",
                {"request_source": fields.get("RequestSource"), "sp_version": version}, run_id,
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
    and a Form submission would wait for the next scheduled sweep. This handles the
    one changed item now, through the same ``_check_item`` and ``sharepoint_seen``
    logic, so a Form request reaches its manager without delay and a rerun (or the
    later scheduled sweep) does nothing. A deleted item is left to the scheduled
    sweep, which flags deletes.

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
        run = SyncRun(job="sharepoint_check")
        session.add(run)
        await session.flush()
        index = await build_staff_index()
        try:
            async with session.begin_nested():                 # isolate this one item
                await _check_item(session, kind, sp_item, index, run.id, counts)
        except Exception:
            counts["errors"] = counts.get("errors", 0) + 1
            logger.exception("SharePoint check (one item): failed on %s item %s", kind, item_id)
        run.counts = counts
        run.finished_at = utcnow()
        await session.commit()
    _wake_outbox()                                             # deliver anything the import queued
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
            index = await build_staff_index()
            for kind, setting_name in KIND_LIST_SETTING.items():
                list_id = getattr(settings, setting_name)
                items = await sp_client.get_list_items(list_id)
                current_ids = {str(item.get("id")) for item in items}
                for sp_item in items:
                    try:
                        async with session.begin_nested():     # isolate one item from the rest
                            await _check_item(session, kind, sp_item, index, run.id, counts)
                    except Exception:
                        counts["errors"] = counts.get("errors", 0) + 1
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
