"""The sync step that brings SharePoint's version history into Postgres (#357).

Runs as one of the shadow sync's steps, after the request import, and once more
in the switch's final sync, so the history moves to Postgres with everything
else. For every request whose SharePoint item changed since it was last read:

- its item versions become its field history (``request_changes``): each field
  that changed, from what to what, who changed it and when (#332);
- an approved or refunded request with no balance record (no audit log, so no
  ledger rows) is matched to the writes in the person's Staff Directory history
  that it made (see ``balance_history``); those writes become its ledger rows,
  and its balance state becomes ``applied`` (or ``reversed`` when the refund's
  writes were found too). When nothing fits, the reason is kept so the "Taken
  from" column can say why.

The shadow sync gives the step a time budget so one run never takes long, and
picks up where it stopped on the next run; each item's ``sp_version`` (stamped by
the importer) tells it whether an item changed since its history was read. The
final sync runs it to the end within the switch's pause, and refuses the switch
when it cannot finish in time, rather than switching with history missing.

SharePoint is only read here, never written, and only before the switch.
"""
import asyncio
import logging
import time
from collections import Counter, defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.balance_ledger import BalanceLedger
from app.models.request_change import RequestChange
from app.models.request_record import RequestRecord
from app.models.sync_run import SyncRun
from app.services import balance_history as bh
from app.services.request_journal import post_ledger

logger = logging.getLogger(__name__)

SHADOW_BUDGET_SECONDS = 240     # per shadow run: the rest waits for the next run, five minutes later
FINAL_BUDGET_SECONDS = 600      # in the switch: well inside its 15-minute pause
READ_CONCURRENCY = 6            # version reads in flight at once; Graph throttles above this
HISTORY_CHANNEL = "import"      # the channel these rows are written under (the SharePoint importer)
HISTORY_REASON = "SharePoint version"   # field-history rows this step owns start with this
LEDGER_KEY_PREFIX = "history:"  # ledger keys this step owns

# Kinds stored -> their SharePoint list.
LIST_FOR_KIND = {
    "leave": settings.SP_LIST_LEAVE_REQUESTS,
    "overtime": settings.SP_LIST_OVERTIME_REQUESTS,
    "carryover_payout": settings.SP_LIST_CARRYOVER_PAYOUT,
}

# Leave types that never move a balance, so there is nothing to find for them.
NO_BALANCE_LEAVE = ("Bereavement", "Jury Duty", "Work from Home")
# Balance states that mean "approved, but nothing proves what it moved".
UNPROVEN = ("not_applied", "legacy_unknown")


class HistoryNotCaughtUp(RuntimeError):
    """The final sync ran out of time before every request's history was read.

    Raised so the switch stops (nothing switched, writes resume) instead of
    switching with history missing; the message says how many are left.
    """


def _needs_reading(record: RequestRecord) -> bool:
    """Whether this request's SharePoint history has not been read at its current version."""
    extra = record.extra or {}
    version = extra.get("sp_version")
    return bool(record.sp_item_id and version) and (extra.get("sp_history") or {}).get("version") != version


def _needs_balance(record: RequestRecord, has_ledger: bool) -> bool:
    """Whether this request is an approval with no record of what it moved."""
    return (
        record.status in ("approved", "refunded")
        and record.balance_state in UNPROVEN
        and not has_ledger
        and (record.leave_type or "") not in NO_BALANCE_LEAVE
    )


def _as_request(record: RequestRecord) -> dict:
    """The fields ``balance_history`` matches on, from a stored request."""
    return {
        "kind": record.kind, "status": record.status,
        "days": float(record.days) if record.days is not None else None,
        "hours": float(record.hours) if record.hours is not None else None,
        "request_option": record.request_option, "start_date": record.start_date,
        "decided_at": record.decided_at, "created_at": record.created_at,
    }


async def _read_all(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """Read many items' versions at once, a few in flight at a time.

    Args:
        pairs: ``(list id, item id)`` to read.

    Returns:
        ``{(list id, item id): versions}``.
    """
    gate = asyncio.Semaphore(READ_CONCURRENCY)

    async def one(pair):
        async with gate:
            return pair, await sp_client.get_item_versions(*pair)

    return dict(await asyncio.gather(*(one(p) for p in pairs)))


async def _used_versions(session: AsyncSession, employee_id: str) -> set[float]:
    """Staff versions already given to a request by an earlier run, for one person.

    Args:
        session: An open session.
        employee_id: The person's staff id.

    Returns:
        The staff version numbers named in this step's ledger keys.
    """
    keys = (await session.execute(
        select(BalanceLedger.idempotency_key).where(
            BalanceLedger.employee_id == employee_id,
            BalanceLedger.idempotency_key.like(f"{LEDGER_KEY_PREFIX}%"),
        )
    )).scalars().all()
    used = set()
    for key in keys:                                           # history:{kind}:{item}:{version}:{pot}
        try:
            used.add(float(key.split(":")[3]))
        except (IndexError, ValueError):
            continue
    return used


def _write_field_history(session: AsyncSession, record: RequestRecord, versions: list[dict]) -> int:
    """Replace this step's field-history rows for one request with a fresh read.

    Args:
        session: An open session; rows are added, not committed.
        record: The request.
        versions: Its SharePoint item versions.

    Returns:
        How many rows were written.
    """
    changes = bh.field_changes(versions)
    for change in changes:
        session.add(RequestChange(
            request_id=record.id, field=change["field"], old_value=change["old"], new_value=change["new"],
            actor=change["by"], channel=HISTORY_CHANNEL,
            reason=f"{HISTORY_REASON} {change['version']:g}",   # which SharePoint version made the change
            changed_at=change["at"],
        ))
    return len(changes)


async def _post_match(session: AsyncSession, record: RequestRecord, match: bh.Match) -> None:
    """Write a matched approval (and refund) to the ledger and mark the request.

    Args:
        session: An open session; not committed here.
        record: The request.
        match: What ``match_request`` found.
    """
    # Who approved: SharePoint's own name when it names a person, else what the request says.
    named = match.approved_by if match.approved_by and match.approved_by != "SharePoint App" else None
    actor = named or record.decided_by or record.manager_name
    label = f"{record.kind.replace('_', '-')} #{record.item_id}"
    for steps, verb in ((match.approval, "approved"), (match.refund, "refund of")):
        for step in steps:
            for pot in step.deltas:                            # one ledger row per pot the write moved
                await post_ledger(
                    session, record, record.employee_id, pot, step.before[pot], step.after[pot],
                    f"{verb} {label} (from SharePoint history, staff version {step.version:g})",
                    actor, HISTORY_CHANNEL,
                    key=f"{LEDGER_KEY_PREFIX}{record.kind}:{record.item_id}:{step.version:g}:{pot}",
                )
    record.balance_state = "reversed" if match.refund else "applied"
    if record.decided_at is None:
        record.decided_at = match.approved_at                  # the moment SharePoint shows it approved
    if record.decided_by is None and named:
        record.decided_by = named


async def _person(session: AsyncSession, employee_id: str, records: list[RequestRecord],
                  versions: dict, ledgers: set[int], counts: Counter) -> None:
    """Bring in the history of one person's changed requests.

    Args:
        session: An open session; not committed here.
        employee_id: The person's staff id (or a non-staff id like "spuser:12").
        records: Their requests whose history needs reading.
        versions: The request versions already read, by (list id, item id).
        ledgers: Request ids that already have ledger rows.
        counts: Running counts, updated in place.
    """
    wanting = [r for r in records if _needs_balance(r, r.id in ledgers)]
    steps: list[bh.BalanceStep] = []
    used: set[int] = set()
    since = None                                               # when their staff history begins
    if wanting and employee_id.isdigit():                      # a staff record to read
        staff_versions = await sp_client.get_item_versions(settings.SP_LIST_STAFF_DIRECTORY, employee_id)
        steps = bh.balance_steps(staff_versions)
        since = bh.history_start(staff_versions)
        taken = await _used_versions(session, employee_id)    # writes an earlier run already gave out
        used = {i for i, step in enumerate(steps) if step.version in taken}

    # Oldest first, so an earlier approval claims its write before a later one could.
    for record in sorted(records, key=lambda r: r.decided_at or r.created_at):
        item_versions = versions.get((LIST_FOR_KIND[record.kind], record.sp_item_id), [])
        await session.execute(delete(RequestChange).where(       # this step's rows only, then a fresh copy
            RequestChange.request_id == record.id,
            RequestChange.channel == HISTORY_CHANNEL,
            RequestChange.reason.like(f"{HISTORY_REASON} %"),
        ))
        counts["field_changes"] += _write_field_history(session, record, item_versions)

        extra = dict(record.extra or {})
        found = dict(extra.get("sp_history") or {})
        found["version"] = extra.get("sp_version")             # read at this version
        if record in wanting:
            request = _as_request(record)
            match = bh.match_request(request, item_versions, steps, used)
            if match:
                await _post_match(session, record, match)
                found["balance"] = "refunded" if match.refund else "matched"
                counts["matched"] += 1
            else:
                found["balance"] = bh.gap_reason(request, item_versions, steps, used, since)
                counts[f"not_matched_{found['balance']}"] += 1
        extra["sp_history"] = found
        record.extra = extra                                   # a new dict so the JSON change is saved
        counts["requests_read"] += 1


async def import_history(session: AsyncSession, budget_seconds: float) -> dict:
    """Read SharePoint history for every changed request, within a time budget.

    Args:
        session: The sync's open session; written to, not committed.
        budget_seconds: Stop starting new people after this long.

    Returns:
        Counts: requests_read, field_changes, matched, not_matched_<reason>,
        and remaining (requests still to read).
    """
    started = time.monotonic()
    records = (await session.execute(
        select(RequestRecord).where(RequestRecord.sp_item_id.is_not(None))
    )).scalars().all()
    todo = [r for r in records if _needs_reading(r)]
    by_person: dict[str, list[RequestRecord]] = defaultdict(list)
    for record in todo:
        by_person[str(record.employee_id)].append(record)
    ledgers = set((await session.execute(select(BalanceLedger.request_id).distinct())).scalars().all())

    counts: Counter = Counter()
    people = sorted(by_person)
    done = 0
    batch = 10                                                 # people whose request versions are read together
    while done < len(people) and time.monotonic() - started < budget_seconds:
        group = people[done:done + batch]
        pairs = [(LIST_FOR_KIND[r.kind], r.sp_item_id) for p in group for r in by_person[p]]
        versions = await _read_all(pairs)
        for person in group:
            await _person(session, person, by_person[person], versions, ledgers, counts)
        await session.flush()                                  # keep the session's pending writes small
        done += len(group)
    counts["remaining"] = sum(len(by_person[p]) for p in people[done:])
    return dict(counts)


async def history_sync_step(session: AsyncSession, sync_run: SyncRun) -> dict:
    """The sync step: a slice each shadow run, everything in the final sync.

    Registered in ``shadow_sync.SYNC_STEPS`` after the request import, so the
    requests it reads are already up to date.

    Args:
        session: The run's open session; the caller commits.
        sync_run: The run's SyncRun row; its job says shadow or final.

    Returns:
        The counts from ``import_history``.

    Raises:
        HistoryNotCaughtUp: In the final sync, when time ran out with requests left.
    """
    final = sync_run is not None and sync_run.job == "final_sync"
    counts = await import_history(session, FINAL_BUDGET_SECONDS if final else SHADOW_BUDGET_SECONDS)
    if final and counts.get("remaining"):
        raise HistoryNotCaughtUp(
            f"SharePoint history is still being copied: {counts['remaining']} requests left. "
            "Nothing was switched. Try the switch again in a few minutes."
        )
    logger.info("History step: %s", counts)
    return counts
