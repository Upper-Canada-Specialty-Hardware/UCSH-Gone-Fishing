"""Copy the three SharePoint request lists into the typed ``requests`` table (#333).

The importer reads every leave, overtime and carry-over/payout item from
SharePoint and writes it into the one typed ``requests`` table, keeping the
SharePoint item id as the item number people and approval links use, so old
links keep working after the switch. It is safe to run again: every request is
upserted by ``(kind, sp_item_id)``, and every event and ledger row it writes
carries an idempotency key, so a second run produces the same rows, not copies.

What one run does, per item:

- resolves who the request is for, by staff id then email, never by display
  name, and records a finding when nothing matches;
- maps the SharePoint columns onto the typed columns, reusing the adapter's
  mapping (``TypedRequestList``) rather than repeating it, and keeps any column
  it does not recognise in ``extra``;
- splits the compound leave Title into description and bereavement relation;
- marks a request decided before 2026-03-23 as legacy, its balance state
  ``legacy_unknown`` unless the audit log proves what happened;
- turns the ``BalanceAuditLog`` into ``balance_ledger`` rows and sets the
  balance state;
- appends an ``imported`` timeline event;
- runs the request-side reconciliation detectors, which only record findings
  for HR and never change anything.

It writes a ``SyncRun`` row with its counts when run on its own, and returns the
same counts when the shadow sync calls it as a step. ``record_finding`` is a
local writer here until the legacy work is joined and the shared
``app/services/legacy_findings.py`` replaces it.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models import request_vocab as vocab
from app.models.legacy_finding import LegacyFinding
from app.models.mixins import utcnow
from app.models.request_counter import RequestCounter
from app.models.request_record import RequestRecord
from app.models.sync_run import SyncRun
from app.repositories.postgres.requests import _stored
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.employment import HOURLY_TYPES, SALARY, employment_type
from app.services.request_journal import append_event, post_ledger, step_key

logger = logging.getLogger(__name__)

# Requests decided before this date predate the current balance rules, so their
# balance effect cannot be trusted without proof from the audit log (#333).
LEGACY_CUTOFF = date(2026, 3, 23)

# kind -> the config setting naming its SharePoint list id.
KIND_LIST_SETTING = {
    "leave": "SP_LIST_LEAVE_REQUESTS",
    "overtime": "SP_LIST_OVERTIME_REQUESTS",
    "carryover_payout": "SP_LIST_CARRYOVER_PAYOUT",
}
# kind -> the person column naming the submitter (internal SharePoint names).
PERSON_COLUMN = {
    "leave": "SubmittedTest",
    "overtime": "SubmittedBy",
    "carryover_payout": "SubmittedBy",
}
# SharePoint balance column -> ledger pot name, for turning the audit log into
# ledger rows and for naming which pots a change touched.
POT_FOR_BALANCE = {
    "CurrentVacationBalance": "vacation",
    "CurrentSickDayBalance": "sick",
    "CurrentOvertimeBalance": "make_up",
    "CarryOver": "carry_over",
    "Payout": "payout",
}
# Leave types that never move a balance (bereavement and jury duty).
NO_COST_LEAVE = ("Bereavement", "Jury Duty")

IMPORT_CHANNEL = "import"       # the channel every imported event and ledger row carries
IMPORT_ACTOR = "import"         # the actor recorded on imported rows


# ----- legacy findings writer (local until the shared one is joined, #337) -----


async def record_finding(
    session: AsyncSession,
    case: str,
    subject: str,
    kind: str | None,
    subject_id: str,
    reason: str,
    evidence: dict,
    run_id: int | None,
) -> LegacyFinding | None:
    """Record one reconciliation finding, at most once per fingerprint.

    The fingerprint ``case:subject:kind:subject_id`` is unique, so a rerun of the
    same check never files a duplicate. This matches the signature of the shared
    ``app/services/legacy_findings.py`` the legacy builder owns; the main session
    swaps this local copy for that one at the join.

    Args:
        session: An open session; not committed here.
        case: One of request_vocab.FINDING_CASES.
        subject: "request" or "staff".
        kind: The request kind, or None for a staff finding.
        subject_id: The item id or staff id, as a string.
        reason: One plain sentence for HR.
        evidence: The JSON backing the finding.
        run_id: The sync_runs row that found it, or None.

    Returns:
        The inserted LegacyFinding, or None when the fingerprint already existed.
    """
    fingerprint = f"{case}:{subject}:{kind}:{subject_id}"       # the dedupe key
    row = LegacyFinding(
        subject=subject,
        kind=kind,
        subject_id=subject_id,
        case=case,
        reason=reason,
        evidence=evidence,
        fingerprint=fingerprint,
        run_id=run_id,
    )
    try:
        async with session.begin_nested():                     # a SAVEPOINT around just this insert
            session.add(row)
            await session.flush()                              # force the INSERT so a clash raises now
        return row
    except IntegrityError:
        # A clash on the fingerprint is a no-op; the caller's other writes stand.
        exists = await session.execute(
            select(LegacyFinding.id).where(LegacyFinding.fingerprint == fingerprint)
        )
        if exists.first() is None:
            raise                                              # not a duplicate: a real error
        return None


# ----- small parsers -----


def _when(value) -> datetime | None:
    """Parse a Graph timestamp ("2026-10-09T14:03:22Z") into an aware datetime.

    Args:
        value: The raw value, or None.

    Returns:
        An aware UTC datetime, or None when missing or unreadable.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _extract_lookup_id(fields: dict, column: str) -> int | None:
    """The SharePoint user id in a person column, in either shape Graph uses.

    Graph returns a person column either as ``<column>LookupId`` or as the
    expanded object under ``<column>`` with a ``LookupId`` key.

    Args:
        fields: The item's fields.
        column: The person column base name, e.g. "SubmittedTest".

    Returns:
        The user id, or None.
    """
    raw = fields.get(f"{column}LookupId")                      # the usual flat shape
    if raw is None and isinstance(fields.get(column), dict):
        raw = fields[column].get("LookupId")                   # the expanded person shape
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


# ----- the submitter index -----


class StaffIndex:
    """Staff and site users loaded once, so each request resolves in memory.

    Resolving a submitter per request with the existing per-call list reads would
    re-fetch the whole Staff Directory thousands of times. This loads both lists
    once and answers from dictionaries.

    Args:
        staff_by_id: Staff Directory item id (str) -> record.
        staff_by_email: Lower-case email -> staff record.
        user_email: SharePoint user id -> that user's email (lower case).
        user_name: SharePoint user id -> that user's display name.
    """

    def __init__(self, staff_by_id, staff_by_email, user_email, user_name):
        self.staff_by_id = staff_by_id
        self.staff_by_email = staff_by_email
        self.user_email = user_email
        self.user_name = user_name

    def resolve(self, kind: str, fields: dict) -> tuple[dict | None, str]:
        """Match a request to its staff record by id, then email, never by name.

        The order is: the request's ``EmployeeID`` as a staff id, then its
        ``SubmitterEmail``, then the person column's user mapped to an email
        through the site's user list. Display-name matching is never used, as
        two people can share a name and names get renamed.

        Args:
            kind: The request kind, which picks the person column.
            fields: The request item's SharePoint fields.

        Returns:
            ``(staff_record_or_None, person_display_name)``; the display name is
            the person column's name, kept for the ``username_as_name`` check and
            as a label when nothing matches.
        """
        person_col = PERSON_COLUMN[kind]
        lookup_id = _extract_lookup_id(fields, person_col)      # the site user who submitted
        display_name = self.user_name.get(lookup_id, "") if lookup_id is not None else ""

        # 1. EmployeeID as a Staff Directory item id (carry-over/payout carries it).
        emp_id = fields.get("EmployeeID")
        if emp_id not in (None, ""):
            staff = self.staff_by_id.get(str(int(emp_id)) if str(emp_id).isdigit() else str(emp_id))
            if staff:
                return staff, display_name

        # 2. SubmitterEmail, the plain-text address the request page writes.
        email = (fields.get("SubmitterEmail") or "").strip().lower()
        if email and email in self.staff_by_email:
            return self.staff_by_email[email], display_name

        # 3. The person column's site user, mapped to an email, then to staff.
        if lookup_id is not None:
            user_email = self.user_email.get(lookup_id, "")
            if user_email and user_email in self.staff_by_email:
                return self.staff_by_email[user_email], display_name

        return None, display_name                               # nothing matched: the caller flags it


async def build_staff_index() -> StaffIndex:
    """Load the Staff Directory and the site user list into one index.

    Returns:
        A StaffIndex built from one read of each list.
    """
    staff_items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    by_id: dict[str, dict] = {}
    by_email: dict[str, dict] = {}
    for item in staff_items:
        by_id[str(item["id"])] = item                           # staff id -> record
        email = (item.get("fields", {}).get("EmailAddress") or "").strip().lower()
        if email:
            by_email[email] = item                              # email -> record
    user_email: dict[int, str] = {}
    user_name: dict[int, str] = {}
    try:
        users = await sp_client.get_list_items("User Information List", top=5000)
    except Exception:
        logger.exception("Request import: could not read the User Information List")
        users = []
    for user in users:
        uid = user.get("id")
        if uid is None:
            continue
        f = user.get("fields", {})
        user_email[int(uid)] = (f.get("EMail") or "").strip().lower()   # Graph spells it EMail
        user_name[int(uid)] = f.get("Title") or ""
    return StaffIndex(by_id, by_email, user_email, user_name)


# ----- the balance audit log -----


@dataclass
class AuditSummary:
    """What the ``BalanceAuditLog`` of one request says happened to its pots."""

    present: bool = False                                       # a parseable, non-empty log existed
    net_changed: bool = False                                  # at least one pot value moved
    has_reverse: bool = False                                  # a refund entry is present
    pots_moved: set = field(default_factory=set)               # ledger pot names that changed
    ledger_rows: list = field(default_factory=list)            # (pot, before, after, reason) per changed key


def parse_balance_audit_log(raw: str | None) -> AuditSummary:
    """Read a ``BalanceAuditLog`` JSON string into a summary and ledger rows.

    The log is a list of entries, each ``{"action", "timestamp", "steps"}``; a
    step is ``{"operation", "before", "after"}`` where before/after map
    SharePoint balance columns to numbers (see services/audit_trail.py). One
    ledger row is produced per balance column that changed in each step, oldest
    first, so the ledger mirrors the order the pots actually moved.

    Args:
        raw: The stored JSON string, or None/blank.

    Returns:
        An AuditSummary; ``present`` is False when the log is missing or
        unreadable, which the caller treats as "no proof".
    """
    summary = AuditSummary()
    entries = _load_entries(raw)
    if not entries:
        return summary
    summary.present = True
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").lower()
        if action == "refund":
            summary.has_reverse = True                          # a reversal was recorded
        for stp in entry.get("steps", []) or []:
            if not isinstance(stp, dict):
                continue
            before = stp.get("before") or {}
            after = stp.get("after") or {}
            operation = stp.get("operation") or action or "balance change"
            for column, pot in POT_FOR_BALANCE.items():
                if column not in before and column not in after:
                    continue
                old = _num(before.get(column))
                new = _num(after.get(column))
                if old == new:
                    continue                                    # this pot did not move in this step
                summary.net_changed = True
                summary.pots_moved.add(pot)
                summary.ledger_rows.append((pot, old, new, f"{action or 'change'}: {operation}".strip()))
    return summary


def _load_entries(raw: str | None) -> list:
    """Parse the audit-log JSON, returning [] for anything unreadable.

    Args:
        raw: The stored JSON string, or None.

    Returns:
        The list of entries, or [] when missing, blank or not a list.
    """
    if not raw or not str(raw).strip():
        return []
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return entries if isinstance(entries, list) else []


def _num(value) -> float:
    """One balance value as a float; blank or unreadable counts as zero.

    Args:
        value: A number, numeric string, None or blank.

    Returns:
        The float, or 0.0.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ----- classification of a request's balance meaning -----


def _is_legacy(decided_at: datetime | None) -> bool:
    """Whether a request was decided before the balance-rules cutoff.

    Args:
        decided_at: When the request was decided, or None if never.

    Returns:
        True when it was decided before 2026-03-23.
    """
    return decided_at is not None and decided_at.date() < LEGACY_CUTOFF


def _no_balance_type(kind: str, leave_type: str | None, emp_type: str | None) -> bool:
    """Whether an approval of this request is expected to move no balance.

    Args:
        kind: The request kind.
        leave_type: The leave type, for leave.
        emp_type: The person's employment type, or None.

    Returns:
        True for bereavement/jury leave and for hourly overtime.
    """
    if kind == "overtime":
        return emp_type in HOURLY_TYPES                         # hourly overtime is paid, not banked
    if kind == "leave":
        return leave_type in NO_COST_LEAVE                      # bereavement and jury cost nothing
    return False                                               # carry-over/payout always moves vacation


def _hourly_vacation(kind: str, leave_type: str | None, emp_type: str | None) -> bool:
    """Whether this is hourly vacation leave, which is counted, not deducted.

    Args:
        kind: The request kind.
        leave_type: The leave type.
        emp_type: The person's employment type, or None.

    Returns:
        True for an hourly person's Vacation leave.
    """
    return kind == "leave" and leave_type == "Vacation" and emp_type in HOURLY_TYPES


def _moves_spendable_pot(kind: str, leave_type: str | None, emp_type: str | None) -> bool:
    """Whether approving this request should move a spendable balance pot.

    This drives ``approved_no_balance_change``: a request that should move a pot
    but whose audit log shows none is the finding. Hourly vacation (counted, not
    deducted) and an unknown employment type are treated as "cannot say", so they
    are not flagged.

    Args:
        kind: The request kind.
        leave_type: The leave type, for leave.
        emp_type: The person's employment type, or None.

    Returns:
        True only when a pot movement is expected.
    """
    if kind == "carryover_payout":
        return True                                            # carry-over and payout both move vacation
    if kind == "overtime":
        return emp_type == SALARY                              # only salaried overtime banks make-up
    # leave
    if emp_type is None:
        return False                                           # unknown type: do not guess a pot moved
    if leave_type in NO_COST_LEAVE:
        return False
    if emp_type in HOURLY_TYPES:
        return leave_type != "Vacation"                        # hourly sick/partial move sick; vacation is counted
    return True                                                # salaried leave of a costing type


def _balance_state(status: str, legacy: bool, no_balance: bool, hourly_vac: bool, audit: AuditSummary) -> str:
    """Decide how far a request's balance effect was carried out.

    Audit-log evidence wins: a refund entry means ``reversed``; any recorded pot
    movement means ``applied``. With no such evidence, an approved request maps
    to ``no_balance``/``counted`` for the types that never spend a pot, and
    otherwise to ``legacy_unknown`` for a legacy request or ``not_applied`` for a
    current one. Non-approved requests are ``legacy_unknown`` when legacy, else
    ``not_applied``.

    Args:
        status: The request's stored status.
        legacy: Whether it was decided before the cutoff.
        no_balance: Whether its type moves no balance.
        hourly_vac: Whether it is hourly vacation (counted).
        audit: The parsed audit log.

    Returns:
        One of request_vocab.BALANCE_STATES.
    """
    if audit.has_reverse:
        return "reversed"                                      # a refund was recorded
    if audit.net_changed:
        return "applied"                                       # the pots actually moved
    if status == "approved":
        if no_balance:
            return "no_balance"                                # approved but this type spends nothing
        if hourly_vac:
            return "counted"                                   # hourly vacation is counted, not deducted
        return "legacy_unknown" if legacy else "not_applied"   # should have moved a pot; audit is silent
    return "legacy_unknown" if legacy else "not_applied"       # pending / rejected / cancelled / refunded w/o reverse


def _looks_like_username(name: str) -> bool:
    """Whether a name reads like a login or email rather than a person's name.

    Args:
        name: The display name to test.

    Returns:
        True for an address, or a single token carrying a dot, underscore or
        digit (e.g. "jdoe2", "john.doe"); False for a normal "First Last".
    """
    text = (name or "").strip()
    if not text:
        return False
    if "@" in text:
        return True                                            # an email address, not a name
    if " " in text:
        return False                                           # a normal multi-word name
    return any(ch.isdigit() for ch in text) or "." in text or "_" in text


# ----- the per-item import -----


def _adapter(kind: str) -> TypedRequestList:
    """The typed-table adapter for a kind, used only for its column mapping.

    Args:
        kind: The request kind.

    Returns:
        A TypedRequestList whose ``_apply_fields`` maps SharePoint columns onto
        a RequestRecord (reused rather than duplicated here).
    """
    return TypedRequestList(kind)


def _fallback_employee_id(fields: dict, person_col: str, sp_item_id: str) -> str:
    """A non-null employee id for a request whose submitter did not match staff.

    ``employee_id`` cannot be null, so an unmatched request still needs a stable
    id. The best available handle is used so HR can trace it, and the request is
    flagged ``submitter_unmatched`` separately.

    Args:
        fields: The request item's fields.
        person_col: The submitter person column for this kind.
        sp_item_id: The SharePoint item id.

    Returns:
        A string id: the EmployeeID column, else the site user id, else the item.
    """
    emp = fields.get("EmployeeID")
    if emp not in (None, ""):
        return str(int(emp)) if str(emp).isdigit() else str(emp)
    lookup = _extract_lookup_id(fields, person_col)
    if lookup is not None:
        return f"spuser:{lookup}"                              # the site user who submitted
    return f"spitem:{sp_item_id}"                              # last resort: the source item


async def _existing_record(session: AsyncSession, kind: str, sp_item_id: str) -> RequestRecord | None:
    """The already-imported row for a SharePoint item, if there is one.

    Args:
        session: An open session.
        kind: The request kind.
        sp_item_id: The SharePoint item id.

    Returns:
        The RequestRecord, or None.
    """
    return (await session.execute(
        select(RequestRecord).where(
            RequestRecord.kind == kind, RequestRecord.sp_item_id == str(sp_item_id)
        )
    )).scalars().first()


async def _import_item(
    session: AsyncSession,
    kind: str,
    sp_item: dict,
    index: StaffIndex,
    run_id: int | None,
    *,
    event_type: str = "imported",
    event_channel: str = IMPORT_CHANNEL,
) -> str:
    """Import or refresh one SharePoint item, then run its detectors.

    Args:
        session: An open session; the caller commits.
        kind: The request kind.
        sp_item: The SharePoint item ({"id", "fields", "createdDateTime", ...}).
        index: The loaded staff index.
        run_id: The sync_runs row this import belongs to, for findings and runs.
        event_type: The timeline event to append ("imported", or "submitted" for
            a Form item seen by the post-switch check).
        event_channel: The channel for that event and the ledger rows.

    Returns:
        "created" when a new row was inserted, "updated" when an existing row was
        refreshed.
    """
    sp_item_id = str(sp_item.get("id"))
    fields = _stored(sp_item.get("fields", {}))                 # drop id/Created/Modified
    person_col = PERSON_COLUMN[kind]
    staff, display_name = index.resolve(kind, fields)

    record = await _existing_record(session, kind, sp_item_id)
    created = record is None
    if record is None:
        record = RequestRecord(kind=kind, item_id=int(sp_item_id), sp_item_id=sp_item_id)

    # Identity first, so the leave-title split sees the right employee name.
    if staff is not None:
        sf = staff.get("fields", {})
        record.employee_id = str(staff["id"])
        record.employee_name = (sf.get("Title") or display_name) or None
        record.employee_email = (sf.get("EmailAddress") or "").strip().lower() or None
        record.employment_type = employment_type(sf) or None    # "" -> None, keeps the CHECK happy
        record.location = sf.get("Location") or record.location
        record.department = sf.get("Department") or record.department
    else:
        record.employee_id = _fallback_employee_id(fields, person_col, sp_item_id)
        record.employee_name = display_name or None
        record.employee_email = (fields.get("SubmitterEmail") or "").strip().lower() or None

    # Reuse the adapter's SharePoint-to-typed mapping, then re-assert identity
    # (the EmployeeID / SubmitterEmail columns it reads must not beat the match).
    adapter = _adapter(kind)
    adapter._apply_fields(record, fields)
    if staff is not None:
        record.employee_id = str(staff["id"])
        record.employee_email = (staff.get("fields", {}).get("EmailAddress") or "").strip().lower() or None
    if fields.get("Managertxt"):
        record.manager_name = fields["Managertxt"]              # the manager's plain-text name

    # Original timestamps, so created_at reflects when SharePoint received it.
    record.created_at = _when(sp_item.get("createdDateTime")) or record.created_at or utcnow()
    record.updated_at = _when(sp_item.get("lastModifiedDateTime")) or record.updated_at or record.created_at

    # Status must be one of the allowed values before the row can be flushed.
    if record.status not in vocab.STATUSES:
        record.status = "pending"

    # Dates that cannot be stored (end before start, negative amounts) are cleared
    # so the row imports, with the raw value preserved in the date_error finding;
    # nothing is silently corrected, it is flagged for HR.
    date_evidence = _date_problems(record, fields)
    if record.end_date and record.start_date and record.end_date < record.start_date:
        record.end_date = None
    for amount in ("days", "hours", "partial_hours"):
        if (getattr(record, amount) or 0) < 0:
            setattr(record, amount, None)

    # Legacy flag and balance state, with audit evidence overriding the default.
    audit = parse_balance_audit_log(fields.get("BalanceAuditLog"))
    record.legacy = _is_legacy(record.decided_at)
    no_balance = _no_balance_type(kind, record.leave_type, record.employment_type)
    hourly_vac = _hourly_vacation(kind, record.leave_type, record.employment_type)
    record.balance_state = _balance_state(record.status, record.legacy, no_balance, hourly_vac, audit)
    if record.decided_at is not None and record.decided_by is None:
        record.decided_by = record.manager_name                 # best available decider for a legacy row

    session.add(record)
    await session.flush()                                      # assign id and enforce the CHECK constraints

    # The audit log becomes ledger rows, keyed so a rerun never doubles them.
    for n, (pot, before, after, reason) in enumerate(audit.ledger_rows):
        await post_ledger(
            session, record, record.employee_id, pot, before, after,
            reason, IMPORT_ACTOR, event_channel,
            key=f"import:{kind}:{record.item_id}:ledger:{n}",
        )

    # One timeline event marking the import (or submission, for a Form item).
    await append_event(
        session, record, event_type, event_channel, IMPORT_ACTOR,
        {"sp_item_id": sp_item_id},
        key=step_key(kind, record.item_id, event_type, record.approval_version),
    )

    await _run_request_detectors(
        session, record, kind, fields, staff, display_name, audit, date_evidence, run_id,
    )
    return "created" if created else "updated"


def _date_problems(record: RequestRecord, fields: dict) -> dict | None:
    """Describe any date or amount error on a request, for the date_error finding.

    Reads the raw SharePoint values so the evidence shows what was actually
    stored, before the importer clears an unstorable value.

    Args:
        record: The record with parsed dates and amounts.
        fields: The raw SharePoint fields.

    Returns:
        An evidence dict when something is wrong, else None.
    """
    problems = []
    if record.start_date and record.end_date and record.end_date < record.start_date:
        problems.append("the end date is before the start date")
    for amount in ("days", "hours", "partial_hours"):
        value = getattr(record, amount)
        if value is not None and value < 0:
            problems.append(f"{amount} is negative")
    # Days larger than the number of days the range covers is impossible.
    if record.start_date and record.end_date and record.days is not None:
        span = (record.end_date - record.start_date).days + 1
        if record.days > span:
            problems.append("more days than the date range covers")
    if not problems:
        return None
    return {
        "problems": problems,
        "start_date": fields.get("StartDate"),
        "end_date": fields.get("EndDate"),
        "days": fields.get("Days"),
    }


# ----- the request-side detectors -----


async def _run_request_detectors(
    session: AsyncSession,
    record: RequestRecord,
    kind: str,
    fields: dict,
    staff: dict | None,
    display_name: str,
    audit: AuditSummary,
    date_evidence: dict | None,
    run_id: int | None,
) -> None:
    """Record any reconciliation findings this request raises. Changes nothing.

    Each detector files at most one finding per case per request. A finding is a
    plain sentence and an evidence JSON for HR to resolve; the importer never
    acts on it.

    Args:
        session: An open session; not committed here.
        record: The imported record.
        kind: The request kind.
        fields: The raw SharePoint fields.
        staff: The matched staff record, or None.
        display_name: The submitter's person-column display name.
        audit: The parsed audit log.
        date_evidence: The date-error evidence, or None.
        run_id: The sync_runs row that found these.
    """
    item_id = str(record.item_id)
    emp_type = record.employment_type
    leave_type = record.leave_type

    async def file(case: str, reason: str, evidence: dict) -> None:
        """Shorthand to record one request finding for this item."""
        await record_finding(session, case, "request", kind, item_id, reason, evidence, run_id)

    # submitter_unmatched: nobody matched by id or email.
    if staff is None:
        await file(
            "submitter_unmatched",
            "The request could not be matched to a staff record by id or email.",
            {"submitter_email": fields.get("SubmitterEmail"), "employee_id": fields.get("EmployeeID"),
             "person_name": display_name},
        )

    # username_as_name: the submitter's name reads like a login or email.
    name_to_test = display_name or (record.employee_name or "")
    if _looks_like_username(name_to_test):
        await file(
            "username_as_name",
            "The submitter's name looks like a username or email, not a person's name.",
            {"name": name_to_test},
        )

    # approved_no_balance_change: approved, should have moved a pot, but did not.
    if record.status == "approved" and _moves_spendable_pot(kind, leave_type, emp_type) and not audit.net_changed:
        await file(
            "approved_no_balance_change",
            "The request was approved but no balance change was recorded.",
            {"leave_type": leave_type, "employment_type": emp_type, "has_audit_log": audit.present},
        )

    # employment_type_suspect: deductions that do not fit the employment type.
    pots = audit.pots_moved
    if emp_type in HOURLY_TYPES:
        salaried_pots = pots - {"sick", "vacation"}             # vacation is hourly_vacation_leftover's case
        if salaried_pots:
            await file(
                "employment_type_suspect",
                "Hourly staff, but the balance change touched a salaried pot.",
                {"employment_type": emp_type, "pots_moved": sorted(pots)},
            )
    elif emp_type == SALARY and kind == "leave" and leave_type == "Vacation":
        if pots and pots <= {"sick"}:
            await file(
                "employment_type_suspect",
                "Salaried vacation, but only the sick pot moved.",
                {"employment_type": emp_type, "pots_moved": sorted(pots)},
            )

    # hourly_vacation_leftover: hourly vacation that was deducted, not counted.
    if emp_type in HOURLY_TYPES and kind == "leave" and leave_type == "Vacation" and "vacation" in pots:
        await file(
            "hourly_vacation_leftover",
            "Hourly vacation was deducted from the vacation pot instead of being counted.",
            {"employment_type": emp_type, "pots_moved": sorted(pots)},
        )

    # date_error: end before start, or an impossible number of days.
    if date_evidence is not None:
        await file(
            "date_error",
            "The request's dates or day count are impossible.",
            date_evidence,
        )

    # stuck_cancelled: cancelled after a balance change that was never reversed.
    if record.status == "cancelled" and audit.net_changed and not audit.has_reverse:
        await file(
            "stuck_cancelled",
            "The request was cancelled after a balance change that was never reversed.",
            {"pots_moved": sorted(pots)},
        )

    # refunded_marked_processed: refunded but still flagged as processed.
    if record.status == "refunded":
        flag = fields.get("ApproveProcessedFlag")
        system_state = fields.get("SystemState")
        if flag == "Processed" or (system_state not in (None, "", "Not Processed")):
            await file(
                "refunded_marked_processed",
                "The request was refunded but is still marked processed.",
                {"approve_processed_flag": flag, "system_state": system_state},
            )


# ----- the run -----


async def _raise_counters(session: AsyncSession, kind: str) -> None:
    """Lift a kind's item-id counter above the largest imported id.

    So a request created in Postgres after the import never reuses an id an
    imported SharePoint item already holds.

    Args:
        session: An open session; not committed here.
        kind: The request kind.
    """
    largest = (await session.execute(
        select(func.max(RequestRecord.item_id)).where(RequestRecord.kind == kind)
    )).scalar()
    if largest is None:
        return                                                 # nothing imported for this kind
    counter = await session.get(RequestCounter, kind)
    if counter is None:
        session.add(RequestCounter(kind=kind, last_item_id=int(largest)))
    elif counter.last_item_id < int(largest):
        counter.last_item_id = int(largest)                    # never lower it


async def _import_all(session: AsyncSession, run_id: int | None) -> dict:
    """Import every item of all three kinds into the typed table.

    Args:
        session: An open session; the caller commits.
        run_id: The sync_runs row this import belongs to.

    Returns:
        Counts per kind plus a total, e.g.
        ``{"leave": {"created": 3, "updated": 0, "errors": 0}, ...}``.
    """
    index = await build_staff_index()
    counts: dict = {}
    for kind, setting_name in KIND_LIST_SETTING.items():
        list_id = getattr(settings, setting_name)
        items = await sp_client.get_list_items(list_id)
        created = updated = errors = 0
        for sp_item in items:
            try:
                async with session.begin_nested():             # isolate one bad item from the rest
                    outcome = await _import_item(session, kind, sp_item, index, run_id)
                created += outcome == "created"
                updated += outcome == "updated"
            except Exception:
                errors += 1
                logger.exception("Request import: failed on %s item %s", kind, sp_item.get("id"))
        await _raise_counters(session, kind)
        counts[kind] = {"source_count": len(items), "created": created, "updated": updated, "errors": errors}
        logger.info("Request import (%s): %d read, %d created, %d updated, %d errors",
                    kind, len(items), created, updated, errors)
    return counts


async def request_import_step(session: AsyncSession, sync_run: SyncRun) -> dict:
    """The shadow-sync step that keeps the typed table in step with SharePoint.

    Registered in ``shadow_sync.SYNC_STEPS``. It writes into the caller's session
    and lets the caller commit, so one shadow run's steps land together.

    Args:
        session: The shadow run's open session.
        sync_run: The shadow run's SyncRun row.

    Returns:
        The import counts, for ``SyncRun.counts``.
    """
    return await _import_all(session, sync_run.id)


async def import_requests() -> dict:
    """Run the request import on its own, writing an ``import`` SyncRun row.

    Used by the admin "import requests" action and any standalone import. The
    shadow sync and the flip call ``request_import_step`` instead, so they can
    group it with the rest of a run.

    Returns:
        The import counts.
    """
    async with async_session() as session:
        run = SyncRun(job="import")
        session.add(run)
        await session.flush()
        try:
            counts = await _import_all(session, run.id)
        except Exception as e:
            run.error = str(e)
            run.finished_at = utcnow()
            await session.commit()
            raise
        run.counts = counts
        run.finished_at = utcnow()
        await session.commit()
    return counts
