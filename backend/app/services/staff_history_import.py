"""Backfill a person's Staff Directory version history into ``staff_changes`` (#448).

Before the switch, HR edited entitlements and balances directly in SharePoint,
and SharePoint kept a version each time. This one-off import reads those kept
versions for every staff record and turns them into ``staff_changes`` rows with
source ``"sharepoint"``, so the person's version history on the dashboard shows
how their numbers reached today's values: the starting value, then every hand
edit, each with its old and new value, who made it, the reason (when a comment
explains it) and the date.

What it writes, per person, in one transaction:

- one "starting value" row per tracked field, from the oldest kept version
  (old value null, the version's value as the new value);
- one row per tracked field that changed between two consecutive versions, with
  the SharePoint editor as the actor and the newer version's time and label.

What it leaves out:

- a whole version written by "SharePoint App" (the app's own writes; their
  balance moves are already in the ledger);
- a balance-pot change in a version the switch matched to an approved request
  (that approval is shown through the request, so it would otherwise show
  twice); entitlement, salary, placement, supervisor and manager changes are
  never dropped for that reason.

It is re-runnable: a person's existing ``"sharepoint"`` rows are deleted and
rewritten in the same transaction; rows from any other source are never touched.

Reads go through the one narrow cut-off exception (``allow_after_switch``): only
the Staff Directory versions read and the site-id lookup it needs are allowed
after the switch; everything else stays refused.
"""
import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.models.staff_change import StaffChange
from app.repositories.staff_store import staff_store
from app.services import balance_history as bh
from app.services.balance_history_import import _used_versions   # reuse: staff versions already given to an approval

logger = logging.getLogger(__name__)

SOURCE = "sharepoint"                       # the channel every row this import writes carries
SHAREPOINT_APP = "SharePoint App"           # the app's own editor name; its versions are skipped
COMMENTS_COLUMN = "Comments"                # the free-text column that may explain a change
MANAGERS_COLUMN = "AllManagers"             # the multi-person approving-managers column
COMMENT_WINDOW = timedelta(hours=1)         # how soon after a change a comment still counts as its reason

READ_CONCURRENCY = 6                        # version reads in flight at once; Graph throttles above this
READ_ATTEMPTS = 3                           # tries per version read before giving up on that person
READ_BACKOFF_SECONDS = 2.0                  # wait before a retry, doubled each time
TRANSIENT_STATUS = (429, 500, 502, 503, 504)   # Graph answers worth trying again

# The five balance pots. A change to one of these is dropped when the version was
# matched to an approval (shown through the request instead).
BALANCE_COLUMNS = set(bh.POT_FOR_COLUMN)

# The two yearly grants, compared as numbers like the pots.
_NUMERIC_COLUMNS = BALANCE_COLUMNS | {"DefaultYearlyVacationDays", "SickDayEntitlement"}

# Every field the history tracks, in the order rows are written for one version,
# so a multi-field version reads in a stable order. The managers column is last
# and handled specially (its value is a list of people, not a scalar).
TRACKED_COLUMNS = [
    "DefaultYearlyVacationDays", "SickDayEntitlement",
    "CurrentVacationBalance", "CurrentSickDayBalance", "CurrentOvertimeBalance",
    "CarryOver", "Payout",
    "SalaryHourly", "Department", "Location", "Supervisor",
    MANAGERS_COLUMN,
]


# ---------------------------------------------------------------------------
# Pure helpers: version shapes in, rows out. No I/O, so they test without a network.
# ---------------------------------------------------------------------------

def _managers_text(value) -> str | None:
    """A readable, order-preserving name list from an AllManagers version value.

    Args:
        value: The ``AllManagers`` field as a version carries it: a list of
            person dicts (``LookupValue`` names), a single dict, a plain string,
            or nothing.

    Returns:
        The names joined with ", ", or None when there are none. The shape Graph
        returns for a person column in a version is not fully pinned down against
        live data, so anything unexpected yields None (no managers row) rather
        than raising.
    """
    if isinstance(value, list):                            # the usual multi-person shape
        names = [str((e or {}).get("LookupValue") or "").strip() for e in value if isinstance(e, dict)]
        names = [n for n in names if n]                    # drop blanks
        return ", ".join(names) or None
    if isinstance(value, dict):                            # a single-person shape
        name = str(value.get("LookupValue") or "").strip()
        return name or None
    if isinstance(value, str):                             # already a plain name
        return value.strip() or None
    return None                                            # absent, or a shape we do not read


def _field_value(version: dict, column: str):
    """One tracked field's value from a version, normalised for comparison/storage.

    Args:
        version: A Graph listItemVersion.
        column: The SharePoint column name.

    Returns:
        A float for a numeric column, a trimmed string for a text column, a
        joined name list for the managers column, or None when the field is
        blank or absent.
    """
    fields = version.get("fields") or {}
    if column == MANAGERS_COLUMN:                          # people, not a scalar
        return _managers_text(fields.get(MANAGERS_COLUMN))
    raw = fields.get(column)
    if raw in (None, ""):                                  # blank -> absent
        return None
    if column in _NUMERIC_COLUMNS:                         # numbers compared as numbers
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None                                    # a stray value never crashes the import
    return str(raw)                                        # text as-is


def _version_number(version: dict) -> float | None:
    """A version's numeric id (e.g. "2.0" -> 2.0), for matching against ledger keys."""
    try:
        return float(version.get("id"))
    except (TypeError, ValueError):
        return None


def _comment(version: dict) -> str | None:
    """The Comments text on a version, trimmed, or None when blank."""
    raw = (version.get("fields") or {}).get(COMMENTS_COLUMN)
    text = str(raw).strip() if raw not in (None, "") else ""
    return text or None


def _within_window(older: dict, newer: dict) -> bool:
    """Whether two versions are within the comment window of each other."""
    return abs(bh._when(newer["lastModifiedDateTime"]) - bh._when(older["lastModifiedDateTime"])) <= COMMENT_WINDOW


def _reason_for(ordered: list[dict], i: int) -> str | None:
    """The comment that explains the change at ``ordered[i]``, if any.

    A change is explained by a comment edited in the same version, or by one the
    same person added in the very next version within an hour (HR often saves the
    number first, then types why).

    Args:
        ordered: The person's versions, oldest first.
        i: The index of the version the change happened at (``i >= 1``).

    Returns:
        The comment text, or None when nothing explains the change.
    """
    newer, older = ordered[i], ordered[i - 1]
    now_comment = _comment(newer)
    if now_comment is not None and now_comment != _comment(older):   # the comment moved in this same write
        return now_comment
    if i + 1 < len(ordered):                               # look one version ahead
        nxt = ordered[i + 1]
        author = bh._author(newer)
        next_comment = _comment(nxt)
        if (author is not None and bh._author(nxt) == author         # the same person
                and _within_window(newer, nxt)                       # soon after
                and next_comment is not None and next_comment != now_comment):   # added a new comment
            return next_comment
    return None


def plan_person_rows(versions: list[dict], used: set[float]) -> list[dict]:
    """The ``staff_changes`` rows one person's SharePoint versions should produce.

    Args:
        versions: The person's Staff Directory listItemVersion objects.
        used: Staff version numbers already given to an approval by the balance
            import, so a balance change in one of those versions is dropped here
            (the approval shows it instead).

    Returns:
        One dict per row: ``{"field", "old_value", "new_value", "actor",
        "reason", "version", "changed_at"}``, oldest first.
    """
    ordered = bh._ordered(versions)                        # oldest first, by version id
    if not ordered:
        return []
    rows: list[dict] = []

    # Starting values: the oldest kept version is the baseline for every field
    # that has a value there (old value null so the UI reads it as a first value).
    oldest = ordered[0]
    oldest_at = bh._when(oldest["lastModifiedDateTime"])
    oldest_actor = bh._author(oldest)
    oldest_label = str(oldest.get("id"))
    for column in TRACKED_COLUMNS:
        value = _field_value(oldest, column)
        if value is None:                                  # no value to seed
            continue
        rows.append({
            "field": column, "old_value": None, "new_value": value,
            "actor": oldest_actor, "reason": None,
            "version": oldest_label, "changed_at": oldest_at,
        })

    # Changes: each consecutive pair, one row per field that moved.
    for i in range(1, len(ordered)):
        newer, older = ordered[i], ordered[i - 1]
        if bh._author(newer) == SHAREPOINT_APP:            # the app's own write: its moves are in the ledger
            continue
        vnum = _version_number(newer)
        label = str(newer.get("id"))
        at = bh._when(newer["lastModifiedDateTime"])
        actor = bh._author(newer)
        reason = _reason_for(ordered, i)
        for column in TRACKED_COLUMNS:
            old, new = _field_value(older, column), _field_value(newer, column)
            if old == new:                                 # this field did not move
                continue
            if column in BALANCE_COLUMNS and vnum is not None and vnum in used:
                continue                                   # this balance move was an approval; shown there
            rows.append({
                "field": column, "old_value": old, "new_value": new,
                "actor": actor, "reason": reason,
                "version": label, "changed_at": at,
            })
    return rows


# ---------------------------------------------------------------------------
# Reads: versions from SharePoint, through the narrow cut-off exception.
# ---------------------------------------------------------------------------

async def _read_versions(item_id: str) -> list[dict] | None:
    """One staff record's versions, retried on a timeout or a busy Graph.

    Args:
        item_id: The Staff Directory item id.

    Returns:
        The versions, or None when every try failed (that person is left for a
        re-run).

    Raises:
        httpx.HTTPStatusError: For a non-transient answer (403 and the like),
            which retrying cannot fix.
    """
    for attempt in range(READ_ATTEMPTS):
        try:
            return await sp_client.get_item_versions(
                settings.SP_LIST_STAFF_DIRECTORY, item_id, allow_after_switch=True,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in TRANSIENT_STATUS:
                raise                                      # a real refusal: let it surface
            problem = f"HTTP {e.response.status_code}"
        except httpx.TransportError as e:                  # timeouts, dropped connections
            problem = type(e).__name__
        if attempt + 1 < READ_ATTEMPTS:
            await asyncio.sleep(READ_BACKOFF_SECONDS * 2 ** attempt)   # 2 s, then 4 s
    logger.warning("Staff history: versions of staff item %s not read (%s)", item_id, problem)
    return None


async def _read_all(item_ids: list[str]) -> dict[str, list[dict] | None]:
    """Read many staff records' versions at once, a few in flight at a time.

    Args:
        item_ids: The Staff Directory item ids to read.

    Returns:
        ``{item id: versions}``; None for an id that could not be read.
    """
    gate = asyncio.Semaphore(READ_CONCURRENCY)             # cap concurrent Graph reads

    async def one(item_id: str):
        async with gate:
            return item_id, await _read_versions(item_id)

    return dict(await asyncio.gather(*(one(i) for i in item_ids)))


async def _staff_people() -> list[tuple[str, str]]:
    """Every staff record with a numeric SharePoint id, as ``(id, name)``.

    Reads from wherever staff live now (Postgres after the switch), so this is a
    database read, not a Graph call. Only numeric ids can have SharePoint
    versions; an app-created person (no SharePoint origin) is skipped.

    Returns:
        ``(sp_item_id, name)`` per person, newest-name-at-import.
    """
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY, include_archived=True)
    people: list[tuple[str, str]] = []
    for item in items:
        sid = str(item.get("id") or "")
        if sid.isdigit():                                  # a SharePoint-origin id
            people.append((sid, str((item.get("fields") or {}).get("Title") or "")))
    return people


async def _write_person(employee_id: str, name: str, versions: list[dict]) -> int:
    """Replace one person's imported rows with a fresh set, in one transaction.

    Args:
        employee_id: The person's Staff Directory id.
        name: Their name now, stored on each row.
        versions: Their Staff Directory versions.

    Returns:
        How many rows were written.
    """
    async with async_session() as session:
        used = await _used_versions(session, employee_id)  # staff versions already given to an approval
        rows = plan_person_rows(versions, used)
        # Drop only this person's SharePoint rows, then insert the fresh set, so a
        # re-run never duplicates and never touches an app-recorded row.
        await session.execute(delete(StaffChange).where(
            StaffChange.employee_id == employee_id, StaffChange.source == SOURCE,
        ))
        for r in rows:
            session.add(StaffChange(
                employee_id=employee_id, employee_name=name,
                field=r["field"], old_value=r["old_value"], new_value=r["new_value"],
                actor=r["actor"], reason=r["reason"], version=r["version"],
                source=SOURCE, changed_at=r["changed_at"],
            ))
        await session.commit()                             # all of this person's rows, or none
    return len(rows)


# ---------------------------------------------------------------------------
# The background run and its in-memory progress.
# ---------------------------------------------------------------------------

class ImportAlreadyRunning(RuntimeError):
    """Asked to start the import while one is already in flight."""


def _blank_progress() -> dict:
    """A fresh progress record for a run that has not started."""
    return {
        "running": False, "started_at": None, "finished_at": None,
        "people_total": 0, "people_done": 0, "rows_written": 0,
        "errors": [], "result": None, "error": None,
    }


_progress: dict = _blank_progress()                        # one backend instance; a restart simply clears it
_task: asyncio.Task | None = None                          # the running background task, if any


def import_progress() -> dict:
    """The current (or last) run's progress, as a copy so a caller cannot mutate it.

    Returns:
        ``{"running", "started_at", "finished_at", "people_total",
        "people_done", "rows_written", "errors", "result", "error"}``.
    """
    snapshot = dict(_progress)
    snapshot["errors"] = list(_progress["errors"])         # copy the list too
    return snapshot


async def run_import() -> dict:
    """Read every staff record's versions and rewrite their imported history.

    Resolves the site id first (the versions URL needs it, and the app does not
    resolve it after the switch), reads all versions with limited concurrency,
    then writes each person in their own transaction, updating the progress as it
    goes.

    Returns:
        The final progress record.
    """
    global _progress
    _progress = _blank_progress()
    _progress.update(running=True, started_at=utcnow().isoformat())
    try:
        if sp_client.site_id is None:                      # resolve the site once, through the exception
            await sp_client.resolve_site_id(allow_after_switch=True)
        people = await _staff_people()
        _progress["people_total"] = len(people)
        versions_by_id = await _read_all([pid for pid, _ in people])   # concurrent reads
        for pid, name in people:
            versions = versions_by_id.get(pid)
            if versions is None:                           # could not be read this run
                _progress["errors"].append({"employee_id": pid, "error": "versions not read"})
            else:
                try:
                    _progress["rows_written"] += await _write_person(pid, name, versions)
                except Exception as e:                     # one person's failure must not stop the rest
                    logger.exception("Staff history: failed to write history for %s", pid)
                    _progress["errors"].append({"employee_id": pid, "error": str(e)})
            _progress["people_done"] += 1
        _progress["result"] = "done"
    except Exception as e:                                 # a run-wide failure (e.g. the site could not resolve)
        logger.exception("Staff history import failed")
        _progress["result"] = "failed"
        _progress["error"] = str(e)
    finally:
        _progress.update(running=False, finished_at=utcnow().isoformat())
    return import_progress()


def start_import() -> None:
    """Start the import as a background task and return at once.

    Raises:
        ImportAlreadyRunning: When a run is already in flight.
    """
    global _task
    if _progress["running"] or (_task is not None and not _task.done()):
        raise ImportAlreadyRunning("The staff history import is already running.")
    _task = asyncio.create_task(run_import())              # fire and forget; progress is polled
