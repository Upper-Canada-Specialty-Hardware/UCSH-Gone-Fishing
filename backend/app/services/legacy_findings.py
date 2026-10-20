"""Record and resolve the reconciliation findings the migration turns up (#337, #341).

Copying years of SharePoint data into Postgres surfaces records that do not add
up: an approved request that moved no balance, a suspect employment type, a
SharePoint change made after the switch. Each is written here with its evidence
and a plain sentence, and HR marks it resolved or ignored. Nothing in this
module changes a balance or a request: HR fixes the underlying record through
the existing hand edit (the person panel for staff; the request's own view for a
request). A finding only tracks that the discrepancy was seen and dealt with.

Three builders write findings. The sync importer and the staff importer call
``record_finding`` with their own open session (it never commits, so the finding
lands with the rest of their transaction); the admin legacy screen reads and
resolves them through the other functions, each of which opens its own session.
The ``fingerprint`` (``case:subject:kind:subject_id``) is unique, so rerunning a
check never files the same finding twice.
"""
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import request_vocab as vocab
from app.models.legacy_finding import LegacyFinding
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)

# The default actor stored when the admin dashboard (a shared password, no named
# user) resolves or ignores a finding.
DEFAULT_ACTOR = "admin"


class FindingError(Exception):
    """A resolve or ignore that cannot proceed, carrying an HTTP status.

    Attributes:
        status_code: The status the route should answer with (400 or 404).
    """

    def __init__(self, message: str, status_code: int = 400):
        """Build the error.

        Args:
            message: The readable reason, shown to the caller.
            status_code: The HTTP status (400 bad input, 404 not found).
        """
        super().__init__(message)
        self.status_code = status_code                         # the route maps this to HTTPException


def _fingerprint(case: str, subject: str, kind: str | None, subject_id: str, variant: str | None = None) -> str:
    """Build the unique fingerprint a rerun dedupes on.

    Args:
        case: The named reconciliation case.
        subject: "request" or "staff".
        kind: The request kind, or None for a staff finding.
        subject_id: The item id or staff id as a string.
        variant: An optional tag for what exactly was found (for example a digest
            of the differing values), so a new difference on the same subject files
            again while the same difference never does; None for one per subject.

    Returns:
        ``case:subject:kind:subject_id`` with an empty slot for a null kind, plus
        ``:variant`` when one is given.
    """
    base = f"{case}:{subject}:{kind or ''}:{subject_id}"       # kind empty for staff findings
    return f"{base}:{variant}" if variant else base            # one finding per distinct variant


def _to_dict(finding: LegacyFinding) -> dict:
    """One finding in the wire shape the admin screen reads.

    Args:
        finding: The ORM row.

    Returns:
        A JSON-serialisable dict; the two timestamps are ISO 8601 or None.
    """
    return {
        "id": finding.id,                                      # for the resolve/ignore calls
        "subject": finding.subject,                            # request or staff
        "kind": finding.kind,                                  # request kind, or null
        "subject_id": finding.subject_id,                      # item id or staff id
        "case": finding.case,                                  # the named case
        "reason": finding.reason,                              # one plain sentence
        "evidence": finding.evidence or {},                    # what backs the finding
        "status": finding.status,                              # open / resolved / ignored
        "note": finding.note,                                  # HR's note, or null
        "resolved_by": finding.resolved_by,                    # who decided, or null
        "resolved_at": finding.resolved_at.isoformat() if finding.resolved_at else None,
        "run_id": finding.run_id,                              # the sync run that found it
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
    }


async def record_finding(
    session: AsyncSession,
    case: str,
    subject: str,
    kind: str | None,
    subject_id: str,
    reason: str,
    evidence: dict | None,
    run_id: int | None = None,
    *,
    variant: str | None = None,
) -> LegacyFinding | None:
    """Write one finding, at most once per fingerprint, without committing.

    The insert runs inside a SAVEPOINT, so a fingerprint clash rolls back only
    this insert and returns None, leaving the caller's outer transaction alive.
    That is how a rerun of the same check never files a duplicate while the
    caller's other writes still commit.

    Args:
        session: An open session; not committed here (the caller commits).
        case: One of request_vocab.FINDING_CASES.
        subject: "request" or "staff".
        kind: The request kind, or None for a staff finding.
        subject_id: The item id or staff id (coerced to a string).
        reason: One plain sentence describing the discrepancy.
        evidence: What backs the finding, JSON-serialisable; None stores ``{}``.
        run_id: The sync_runs row that found it, or None.
        variant: Optional tag making the fingerprint content-specific (see
            ``_fingerprint``); None keeps one finding per case and subject.

    Returns:
        The inserted LegacyFinding, or None when the fingerprint already existed.
    """
    fingerprint = _fingerprint(case, subject, kind, str(subject_id), variant)   # the dedupe key
    row = LegacyFinding(
        subject=subject,
        kind=kind,
        subject_id=str(subject_id),
        case=case,
        reason=reason,
        evidence=evidence or {},                               # never null; the column defaults to {}
        status="open",                                         # every finding starts open
        fingerprint=fingerprint,
        run_id=run_id,
    )
    try:
        async with session.begin_nested():                     # a SAVEPOINT around just this insert
            session.add(row)
            await session.flush()                              # force the INSERT so a clash raises now
        return row
    except IntegrityError:
        # Only a clash on the unique fingerprint is a no-op; any other failure
        # (a CHECK on case/subject/status) is a real error and must surface.
        existing = await session.execute(
            select(LegacyFinding.id).where(LegacyFinding.fingerprint == fingerprint)
        )
        if existing.first() is None:
            raise                                              # not a duplicate: the real error stands
        return None                                            # already filed: a no-op


async def _hidden_finding_ids(session: AsyncSession) -> list[int]:
    """Findings kept in the table but not shown on the legacy data screen.

    Old carry-overs and payouts, decided before the app approved anything, were
    moved by hand or by the April reset and never wrote a balance log, so their
    "approved but no balance change recorded" finding says nothing. The importer
    no longer files these (``_old_carryover_without_log``); the ones filed before
    that rule stay in the table, untouched, and are only left out of the list
    and the counts. The rule is the importer's: no audit log, and decided (or,
    with no decision date, created) before the cut-off.

    Args:
        session: An open session.

    Returns:
        The ids of the findings to leave out.
    """
    from app.models.request_record import RequestRecord          # lazy: the importer imports this module
    from app.services.request_import import LEGACY_CUTOFF         # lazy: same cycle
    candidates = (await session.execute(
        select(LegacyFinding.id, LegacyFinding.subject_id, LegacyFinding.evidence).where(
            LegacyFinding.case == "approved_no_balance_change",
            LegacyFinding.kind == "carryover_payout",
        )
    )).all()
    no_log = {
        subject_id: finding_id for finding_id, subject_id, evidence in candidates
        if not (evidence or {}).get("has_audit_log")              # a log that shows no move stays visible
    }
    if not no_log:
        return []
    item_ids = [int(sid) for sid in no_log if str(sid).isdigit()]   # subject_id is the item id as text
    requests = (await session.execute(
        select(RequestRecord.item_id, RequestRecord.decided_at, RequestRecord.created_at).where(
            RequestRecord.kind == "carryover_payout",
            RequestRecord.item_id.in_(item_ids),
        )
    )).all()
    hidden = []
    for item_id, decided_at, created_at in requests:
        when = decided_at or created_at                           # old rows may lack a decision date
        if when is not None and when.date() < LEGACY_CUTOFF:     # from before the app approved anything
            hidden.append(no_log[str(item_id)])
    return hidden


async def list_findings(
    case: str | None = None,
    status: str | None = "open",
    subject: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """A page of findings matching the filters, newest first.

    Args:
        case: Keep only this case, or None for every case.
        status: Keep only this status (open / resolved / ignored), or None for all.
        subject: Keep only "request" or "staff", or None for both.
        page: 1-based page number.
        page_size: Rows per page.

    Returns:
        ``{"findings": [...], "total": int, "page": int, "page_size": int}``.
    """
    offset = max(0, (page - 1) * page_size)                    # rows to skip for this page
    conditions = []                                            # built once, applied to count and page
    if status:
        conditions.append(LegacyFinding.status == status)
    if case:
        conditions.append(LegacyFinding.case == case)
    if subject:
        conditions.append(LegacyFinding.subject == subject)
    async with async_session() as session:
        hidden = await _hidden_finding_ids(session)            # old carry-overs: kept, not shown
        if hidden:
            conditions.append(LegacyFinding.id.not_in(hidden))
        count_query = select(func.count()).select_from(LegacyFinding)
        page_query = select(LegacyFinding)
        for condition in conditions:                           # same filters on both queries
            count_query = count_query.where(condition)
            page_query = page_query.where(condition)
        total = (await session.execute(count_query)).scalar_one()
        result = await session.execute(
            page_query
            .order_by(LegacyFinding.created_at.desc(), LegacyFinding.id.desc())   # newest first, stable
            .offset(offset)
            .limit(page_size)
        )
        findings = [_to_dict(row) for row in result.scalars().all()]
    return {"findings": findings, "total": int(total), "page": page, "page_size": page_size}


async def counts_by_case(status: str | None = "open") -> dict[str, int]:
    """How many findings each case has at the given status.

    Args:
        status: The status to count (open / resolved / ignored), or None for all.

    Returns:
        Every case in request_vocab.FINDING_CASES mapped to its count, zero
        included, so the screen can show a badge on each case even at zero.
    """
    query = select(LegacyFinding.case, func.count()).group_by(LegacyFinding.case)
    if status:
        query = query.where(LegacyFinding.status == status)
    async with async_session() as session:
        hidden = await _hidden_finding_ids(session)            # old carry-overs: kept, not counted
        if hidden:
            query = query.where(LegacyFinding.id.not_in(hidden))
        rows = (await session.execute(query)).all()            # [(case, n), ...]
    counts = {case: 0 for case in vocab.FINDING_CASES}         # start every case at zero
    for case, number in rows:
        counts[case] = int(number)                             # fill the ones that have findings
    return counts


async def _decide(finding_id: int, note: str, actor: str | None, status: str) -> dict:
    """Mark one finding resolved or ignored, recording the note and who.

    Args:
        finding_id: The finding's id.
        note: HR's note; required (an empty one is refused).
        actor: Who decided; defaults to the shared admin dashboard.
        status: "resolved" or "ignored".

    Returns:
        The updated finding as a dict.

    Raises:
        FindingError: 400 when the note is empty, 404 when the finding is gone.
    """
    note = (note or "").strip()                                # trim, so whitespace is not a note
    if not note:
        raise FindingError("A note is required.")              # 400: the UI also enforces this
    async with async_session() as session:
        finding = (
            await session.execute(select(LegacyFinding).where(LegacyFinding.id == finding_id))
        ).scalar_one_or_none()
        if finding is None:
            raise FindingError("That finding was not found.", 404)
        finding.status = status                                # open -> resolved/ignored
        finding.note = note
        finding.resolved_by = (actor or "").strip() or DEFAULT_ACTOR   # record who, admin by default
        finding.resolved_at = utcnow()                         # and when
        await session.commit()
        return _to_dict(finding)


async def resolve(finding_id: int, note: str, actor: str | None = None) -> dict:
    """Mark a finding resolved: HR fixed the underlying record.

    Args:
        finding_id: The finding's id.
        note: HR's note; required.
        actor: Who resolved it; defaults to the admin dashboard.

    Returns:
        The updated finding as a dict.

    Raises:
        FindingError: 400 with no note, 404 when the finding is gone.
    """
    return await _decide(finding_id, note, actor, "resolved")


async def ignore(finding_id: int, note: str, actor: str | None = None) -> dict:
    """Mark a finding ignored: the discrepancy is expected or not worth fixing.

    Args:
        finding_id: The finding's id.
        note: HR's note; required.
        actor: Who ignored it; defaults to the admin dashboard.

    Returns:
        The updated finding as a dict.

    Raises:
        FindingError: 400 with no note, 404 when the finding is gone.
    """
    return await _decide(finding_id, note, actor, "ignored")
