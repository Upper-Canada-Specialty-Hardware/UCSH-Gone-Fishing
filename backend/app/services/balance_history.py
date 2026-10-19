"""Rebuild old requests' balance records and field history from SharePoint's version history.

SharePoint keeps every version of every list item. Two of those histories
together say what an old approval really did, even when the request never got a
``BalanceAuditLog``:

- the request item's versions say *when* it was approved (the version whose
  Status first reads "Approved") and by whom, and every field change it went
  through, which becomes the request's field history (#332);
- the person's Staff Directory versions say what their five balance pots were
  before and after every write, so the write that happened at that moment, and
  that moved the pots the way this kind of request moves them, is the approval's
  real balance movement.

This module is pure (no I/O): it takes the version lists Graph returns and gives
back the steps, the approval moment, the field changes and the match. The runner
that reads SharePoint and writes the ledger lives in ``balance_history_import``.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Staff Directory column -> ledger pot name.
POT_FOR_COLUMN = {
    "CurrentVacationBalance": "vacation",
    "CurrentSickDayBalance": "sick",
    "CurrentOvertimeBalance": "make_up",
    "CarryOver": "carry_over",
    "Payout": "payout",
}

# Request-item columns that are SharePoint's own bookkeeping, never a field a
# person changed; left out of the field history.
SYSTEM_FIELDS = {
    "id", "ID", "Modified", "Created", "Author", "Editor", "AuthorLookupId", "EditorLookupId",
    "AppAuthorLookupId", "AppEditorLookupId", "ContentType", "Attachments", "Edit", "LinkTitle",
    "LinkTitleNoMenu", "ItemChildCount", "FolderChildCount", "_UIVersionString", "_ComplianceFlags",
    "_ComplianceTag", "_ComplianceTagWrittenTime", "_ComplianceTagUserId", "ComplianceAssetId",
    "DocIcon", "_IsRecord", "BalanceAuditLog",        # the audit log is JSON noise; its moves go to the ledger
}

# A balance this large is a placeholder someone typed into the Staff Directory
# (one record held 100,000,000,000 days of Make-Up for months), not a real
# balance; real ones stay near 100. A write that moved a placeholder pot is not
# evidence of anything, and its numbers do not fit the ledger's columns.
PLACEHOLDER_DAYS = 10_000

TOLERANCE = 0.002                      # days: 0.5 h of overtime is 0.0625 d, so this is well inside a real gap
RUN_SECONDS = 120                      # a few writes this close together are one approval
# A leave approved before its request item's history begins (the lists were
# loaded into SharePoint in bulk in late September 2024) has no approval moment.
# Its deduction is then looked for between this long before its first day and
# START_AFTER after it, and taken only when exactly one write fits.
START_BEFORE = timedelta(days=45)
START_AFTER = timedelta(days=2)
WINDOWS = (                            # how far from the approval moment to look, nearest first
    timedelta(minutes=10),
    timedelta(hours=6),
    timedelta(days=3),
)


@dataclass
class BalanceStep:
    """One Staff Directory write that moved at least one pot.

    Attributes:
        version: The version number SharePoint gave the write (orders the steps).
        at: When the write happened (UTC).
        by: Who SharePoint says wrote it ("SharePoint App" for the app itself).
        before: Pot values before the write.
        after: Pot values after the write.
    """

    version: float
    at: datetime
    by: str | None
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

    @property
    def deltas(self) -> dict[str, float]:
        """Signed change per pot, only the pots that moved."""
        moved = {}
        for pot in POT_FOR_COLUMN.values():
            change = round(self.after.get(pot, 0.0) - self.before.get(pot, 0.0), 4)
            if change:
                moved[pot] = change
        return moved


@dataclass
class Match:
    """The steps one request's approval (and, when refunded, its refund) made.

    Attributes:
        approval: The steps of the approval, in order.
        refund: The steps of the refund, empty when none was found.
        approved_at: The approval moment the match was anchored on.
        approved_by: Who set Status to Approved, when SharePoint says.
        window: How far from that moment the steps were found; None when it
            was found by the leave's first day instead.
    """

    approval: list[BalanceStep]
    refund: list[BalanceStep]
    approved_at: datetime
    approved_by: str | None
    window: timedelta | None


def _when(value: str) -> datetime:
    """A Graph timestamp ("2026-10-15T11:31:29Z") as an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _number(value) -> float:
    """A pot value as a float; blank or unreadable counts as zero."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ordered(versions: list[dict]) -> list[dict]:
    """Versions oldest first, by version number (times can tie or run backwards)."""
    return sorted(versions, key=lambda v: float(v.get("id") or 0))


def _author(version: dict) -> str | None:
    """Who SharePoint says made a version."""
    return ((version.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")


def balance_steps(staff_versions: list[dict]) -> list[BalanceStep]:
    """Turn a person's Staff Directory versions into the writes that moved a pot.

    Args:
        staff_versions: Graph listItemVersion objects (``id``,
            ``lastModifiedDateTime``, ``lastModifiedBy``, ``fields``).

    Returns:
        One BalanceStep per consecutive pair of versions where a pot changed,
        oldest first. Edits that touched no pot are dropped, and so are writes
        that moved a pot holding a placeholder (``PLACEHOLDER_DAYS`` or more).
    """
    steps = []
    ordered = _ordered(staff_versions)
    for older, newer in zip(ordered, ordered[1:]):           # each write is a pair of neighbours
        before = {pot: _number((older.get("fields") or {}).get(col)) for col, pot in POT_FOR_COLUMN.items()}
        after = {pot: _number((newer.get("fields") or {}).get(col)) for col, pot in POT_FOR_COLUMN.items()}
        step = BalanceStep(float(newer["id"]), _when(newer["lastModifiedDateTime"]), _author(newer), before, after)
        moved = step.deltas
        if not moved:
            continue                                          # a write that moved no pot is not a balance step
        if any(max(abs(before[pot]), abs(after[pot])) >= PLACEHOLDER_DAYS for pot in moved):
            continue                                          # a placeholder pot moved: not a real balance change
        steps.append(step)
    return steps


def status_moment(request_versions: list[dict], status: str) -> tuple[datetime | None, str | None]:
    """When a request item's Status first read ``status``, and who wrote it.

    Args:
        request_versions: The request item's Graph versions.
        status: "Approved", "Refunded", ...

    Returns:
        (moment, author), or (None, None) when no version has that status.
    """
    for version in _ordered(request_versions):
        if ((version.get("fields") or {}).get("Status") or "").strip() == status:
            return _when(version["lastModifiedDateTime"]), _author(version)
    return None, None


def field_changes(request_versions: list[dict]) -> list[dict]:
    """Every field a request item went through, version to version (#332).

    Args:
        request_versions: The request item's Graph versions.

    Returns:
        ``{"version", "field", "old", "new", "at", "by"}`` per changed field,
        oldest first; SharePoint's own bookkeeping columns are left out.
    """
    changes = []
    ordered = _ordered(request_versions)
    for older, newer in zip(ordered, ordered[1:]):
        old_fields, new_fields = older.get("fields") or {}, newer.get("fields") or {}
        for name in sorted(set(old_fields) | set(new_fields)):
            if name in SYSTEM_FIELDS or name.startswith(("@", "_", "OData")):
                continue                                      # SharePoint's own columns
            old, new = old_fields.get(name), new_fields.get(name)
            if old != new:
                changes.append({
                    "version": float(newer["id"]), "field": name, "old": old, "new": new,
                    "at": _when(newer["lastModifiedDateTime"]), "by": _author(newer),
                })
    return changes


def _close(a: float, b: float) -> bool:
    """Two day amounts equal to within TOLERANCE."""
    return abs(a - b) <= TOLERANCE


def looks_like_approval(request: dict, net: dict[str, float]) -> bool:
    """Whether a net pot change is the one approving this request makes.

    Args:
        request: ``kind``, ``days``, ``hours`` and ``request_option`` of the request.
        net: The signed change per pot of one write, or of a short run of writes.

    Returns:
        True when the change has this request's shape: overtime adds its hours
        to Make-Up (which may then pay off negative vacation, so Make-Up plus
        vacation rose by the hours); carry-over or payout moves its days out of
        vacation into that pot; leave takes exactly its days out of the pots,
        and puts nothing back.
    """
    kind = request["kind"]
    if kind == "overtime":
        earned = _number(request.get("hours")) / 8           # hours to days, as the balance engine does
        if set(net) - {"make_up", "vacation"} or net.get("vacation", 0) < 0:
            return False                                      # overtime only adds, and only to these two
        return earned > 0 and _close(net.get("make_up", 0) + net.get("vacation", 0), earned)
    days = _number(request.get("days"))
    if kind == "carryover_payout":
        to = "payout" if (request.get("request_option") or "").lower().startswith("pay") else "carry_over"
        return days > 0 and _close(net.get("vacation", 0), -days) and _close(net.get(to, 0), days)
    taken = sum(v for v in net.values() if v < 0)            # leave: days out of one or more pots
    returned = sum(v for v in net.values() if v > 0)
    return days > 0 and _close(taken, -days) and returned == 0


def looks_like_refund(request: dict, net: dict[str, float]) -> bool:
    """Whether a net pot change puts this request's approval back.

    Args:
        request: As for looks_like_approval.
        net: The signed change per pot.

    Returns:
        True for the mirror image of an approval: the same amounts, opposite sign.
    """
    return looks_like_approval(request, {pot: -change for pot, change in net.items()})


def _net(steps: list[BalanceStep]) -> dict[str, float]:
    """The summed change per pot of a run of steps, zero pots dropped."""
    total: dict[str, float] = {}
    for step in steps:
        for pot, change in step.deltas.items():
            total[pot] = total.get(pot, 0.0) + change
    return {pot: round(v, 4) for pot, v in total.items() if round(v, 4)}


def _find(request, steps, used, anchor, window, test) -> tuple[timedelta, list[int]] | None:
    """The unused run of steps nearest ``anchor`` that passes ``test``.

    Args:
        request: The request being matched.
        steps: The person's balance steps, oldest first.
        used: Indexes of steps already given to another request.
        anchor: The approval (or refund) moment.
        window: How far from ``anchor`` a run may start.
        test: looks_like_approval or looks_like_refund.

    Returns:
        (distance, step indexes), or None when nothing fits.
    """
    best = None
    for i, first in enumerate(steps):
        if i in used or abs(first.at - anchor) > window:
            continue
        fits = None                                           # the longest run from here that fits
        for length in (1, 2, 3):                              # one write, or a few close together
            run = list(range(i, min(i + length, len(steps))))
            if len(run) < length or any(j in used for j in run):
                break
            if (steps[run[-1]].at - first.at).total_seconds() > RUN_SECONDS:
                break                                         # too far apart to be one approval
            if test(request, _net([steps[j] for j in run])):
                fits = run                                    # keep going: a longer run may still fit
        if fits:
            # The longest run that fits: an old flow sometimes took the days from
            # one pot, then moved them to another a second later; all of it is the
            # approval, and leaving the later writes out would book the wrong pot.
            distance = abs(first.at - anchor)
            if best is None or distance < best[0]:
                best = (distance, fits)
    return best


def born_decided(request_versions: list[dict]) -> bool:
    """Whether the request item's oldest kept version is already decided.

    Args:
        request_versions: The request item's Graph versions.

    Returns:
        True when its history begins after it was approved (or refunded), so
        the history cannot say when that happened.
    """
    ordered = _ordered(request_versions)
    first = ((ordered[0].get("fields") or {}).get("Status") or "").strip() if ordered else ""
    return first in ("Approved", "Refunded")


def _by_start_date(request: dict, steps: list[BalanceStep], used: set[int]) -> list[int] | None:
    """The one unused write before a leave's first day that has its shape.

    Args:
        request: The leave, with ``start_date`` (a date) and ``days``.
        steps: The person's balance steps, oldest first.
        used: Indexes of steps already matched.

    Returns:
        ``[index]`` when exactly one write fits; None when none or several do,
        so an ambiguous one is never guessed.
    """
    start_day = request.get("start_date")
    if request.get("kind") != "leave" or start_day is None or not steps:
        return None
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=steps[0].at.tzinfo)
    fits = [i for i, step in enumerate(steps)
            if i not in used and start - START_BEFORE <= step.at <= start + START_AFTER
            and looks_like_approval(request, step.deltas)]
    return fits if len(fits) == 1 else None


def match_request(
    request: dict, request_versions: list[dict], steps: list[BalanceStep], used: set[int],
) -> Match | None:
    """Find the balance writes one approved (or refunded) request made.

    The approval moment is when the request item's Status first read
    "Approved"; without that version, the stored decision time, then the
    creation time. The writes nearest that moment, within the widening
    ``WINDOWS``, that have this request's shape are its approval. A refunded
    request also looks for the mirror-image writes near its "Refunded" moment.
    Steps found are added to ``used`` so two requests never claim one write.

    A leave whose request history begins after it was already approved has no
    approval moment; it is matched by its first day instead, and only when
    exactly one write fits.

    Args:
        request: ``kind``, ``days``, ``hours``, ``request_option``, ``status``,
            ``start_date``, ``decided_at`` and ``created_at`` of the request.
        request_versions: The request item's Graph versions (may be empty).
        steps: The person's balance steps, oldest first.
        used: Indexes of steps already matched; updated in place.

    Returns:
        The Match, or None when no write fits.
    """
    approved_at, approved_by = status_moment(request_versions, "Approved")
    anchor = approved_at or request.get("decided_at") or request.get("created_at")
    if anchor is None:
        return None
    hit, window = None, None
    if born_decided(request_versions):                       # no approval moment in the history
        only = _by_start_date(request, steps, used)
        if only:
            # The write is the best moment there is; who approved is unknown
            # (the oldest version is a bulk load, not the decision).
            hit, approved_at, approved_by = (timedelta(0), only), steps[only[0]].at, None
    else:
        for window in WINDOWS:                               # nearest first
            hit = _find(request, steps, used, anchor, window, looks_like_approval)
            if hit:
                break
    if hit is None:
        return None
    used.update(hit[1])
    refund_steps: list[BalanceStep] = []
    if request.get("status") == "refunded":                  # the approval was put back later
        refunded_at, _ = status_moment(request_versions, "Refunded")
        after_approval = steps[hit[1][-1]].at
        for refund_window in WINDOWS:
            found = _find(request, steps, used, refunded_at or after_approval, refund_window, looks_like_refund)
            if found and steps[found[1][0]].at >= after_approval:
                used.update(found[1])
                refund_steps = [steps[j] for j in found[1]]
                break
    return Match(
        approval=[steps[j] for j in hit[1]], refund=refund_steps,
        approved_at=approved_at or anchor, approved_by=approved_by, window=window,
    )


# Why an approval could not be matched, for the "Taken from" column.
GAP_NO_STAFF_RECORD = "no_staff_record"       # the person has no Staff Directory record to read
GAP_BEFORE_RECORDS = "before_records"         # approved before the history SharePoint kept
GAP_AMBIGUOUS = "ambiguous"                   # several writes fit; none is picked rather than guess
GAP_NO_MATCH = "no_match"                     # the history has no write of this request's shape


def history_start(staff_versions: list[dict]) -> datetime | None:
    """When a person's kept Staff Directory history begins (its oldest version).

    Args:
        staff_versions: The staff record's Graph versions.

    Returns:
        The oldest version's time, or None when there are no versions.
    """
    ordered = _ordered(staff_versions)
    return _when(ordered[0]["lastModifiedDateTime"]) if ordered else None


def gap_reason(
    request: dict, request_versions: list[dict], steps: list[BalanceStep], used: set[int],
    since: datetime | None = None,
) -> str:
    """Say why ``match_request`` found nothing for this request.

    Args:
        request: As for match_request.
        request_versions: The request item's Graph versions.
        steps: The person's balance steps, oldest first.
        used: Indexes of steps already matched.
        since: When the staff history begins (``history_start``); the first
            step's time when not given.

    Returns:
        One of the GAP_* codes.
    """
    if not steps:
        return GAP_NO_STAFF_RECORD                           # no staff record, or one that never moved a pot
    if born_decided(request_versions):                       # no approval moment: only its first day to go on
        start_day = request.get("start_date")
        if request.get("kind") == "leave" and start_day is not None:
            start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=steps[0].at.tzinfo)
            fits = [i for i, step in enumerate(steps)
                    if i not in used and start - START_BEFORE <= step.at <= start + START_AFTER
                    and looks_like_approval(request, step.deltas)]
            if len(fits) > 1:
                return GAP_AMBIGUOUS
        return GAP_BEFORE_RECORDS                            # its own history starts after the decision
    approved_at, _ = status_moment(request_versions, "Approved")
    anchor = approved_at or request.get("decided_at") or request.get("created_at")
    if anchor is not None and anchor < (since or steps[0].at):
        return GAP_BEFORE_RECORDS                            # decided before the staff history begins
    return GAP_NO_MATCH
