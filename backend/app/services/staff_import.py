"""Copy the Staff Directory into Postgres, idempotently, and flag what is off (#261, #341).

``staff_sync_step`` reads every Staff Directory record from SharePoint and writes
it into the ``staff`` table (and its location, department and supervisor-link
tables), keyed on the SharePoint item id so a second run updates the same people
instead of duplicating them. One bad record never stops the run: each is written
inside its own savepoint, and a record that cannot be written is counted and
reported rather than aborting the import.

As it goes it runs the #341 detectors, which flag a record for HR to look at and
never change it: an employment type that does not fit the balances, a login used
as a name, a person with no Microsoft 365 link, and a below-zero balance the
current rules would not have produced. Each flag is one ``legacy_findings`` row,
filed once per case per person (the fingerprint makes reruns safe).

The findings writer here is a small local stand-in with the shared contract
signature; the migration's real ``legacy_findings.record_finding`` replaces it at
the join.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.legacy_finding import LegacyFinding
from app.models.staff import Staff, StaffSupervisor
from app.repositories.postgres.staff import _lookup_id, _norm_email, apply_sp_fields, staff_query
from app.services.employment import is_hourly

logger = logging.getLogger(__name__)

# The #341 staff cases, each a value the legacy_findings CHECK allows.
CASE_EMPLOYMENT_TYPE_SUSPECT = "employment_type_suspect"
CASE_USERNAME_AS_NAME = "username_as_name"
CASE_NO_USER_LINK = "no_user_link"
CASE_BELOW_ZERO_PREDATES_RULES = "below_zero_predates_rules"

# The five pots and their SharePoint columns, for the below-zero detector.
_POT_COLUMNS = {
    "sick": "CurrentSickDayBalance",
    "make_up": "CurrentOvertimeBalance",
    "carry_over": "CarryOver",
    "vacation": "CurrentVacationBalance",
    "payout": "Payout",
}


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
    """Insert one finding, skipping it when the same case was already filed. No commit.

    The fingerprint ``case:subject:kind:subject_id`` is unique, so a rerun of the
    same detector never files a duplicate. This is the local stand-in the staff
    work uses until the migration's shared ``legacy_findings.record_finding`` is
    joined; the signature matches that one exactly.

    Args:
        session: The caller's open session; nothing is committed here.
        case: One of the legacy_findings cases.
        subject: "request" or "staff".
        kind: The request kind, or None for a staff finding.
        subject_id: The item id or staff id the finding is about, as a string.
        reason: One plain sentence for HR.
        evidence: What backs the finding (JSON-serialisable).
        run_id: The sync_runs row that found it, if any.

    Returns:
        The new finding row, or None when an identical one already existed.
    """
    fingerprint = f"{case}:{subject}:{kind or ''}:{subject_id}"      # the unique key
    already = (await session.execute(
        select(LegacyFinding).where(LegacyFinding.fingerprint == fingerprint)
    )).scalars().first()
    if already is not None:                                          # same case, same person: skip
        return None
    row = LegacyFinding(
        subject=subject,
        kind=kind,
        subject_id=str(subject_id),
        case=case,
        reason=reason,
        evidence=evidence or {},
        status="open",
        fingerprint=fingerprint,
        run_id=run_id,
    )
    session.add(row)
    return row


def _num(value) -> float:
    """A balance value as a float, blank counting as zero.

    Args:
        value: A number column value or None.

    Returns:
        The value as a float, or 0.0 when blank or unreadable.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_username(name: str, email: str) -> bool:
    """Whether a name looks like a login rather than a person's name.

    Fires when the name holds an email address, equals the email's login part,
    or is a single spaceless token carrying a digit (e.g. "jsmith2"). HR then
    confirms and fixes the real name; the importer never changes it.

    Args:
        name: The Staff Directory Title.
        email: The EmailAddress, used to spot a name equal to the login part.

    Returns:
        True when the name reads like a username.
    """
    cleaned = (name or "").strip()
    if not cleaned:                                                  # a blank name is a different problem
        return False
    if "@" in cleaned:                                              # an email dropped in the name field
        return True
    local = (email or "").split("@")[0].strip().lower()            # the login part of the email
    if local and cleaned.lower() == local:                         # the name IS the login
        return True
    if " " not in cleaned and any(c.isdigit() for c in cleaned):   # a spaceless token with a digit
        return True
    return False


async def _run_detectors(session: AsyncSession, sp_id: str, fields: dict, run_id: int | None) -> int:
    """Run the four #341 staff detectors on one record, filing a finding each.

    Args:
        session: The open session; findings are added, not committed.
        sp_id: The person's Staff Directory item id.
        fields: The record's SharePoint fields.
        run_id: The sync run that is importing, for the finding rows.

    Returns:
        How many new findings were filed for this person (skipped duplicates do
        not count).
    """
    filed = 0
    name = (fields.get("Title") or "").strip()
    email = (fields.get("EmailAddress") or "").strip()

    # 1. Employment type suspect: hourly staff get no vacation entitlement and
    # carry no vacation balance, so either one on an hourly record is suspect.
    if is_hourly(fields):
        entitlement = _num(fields.get("DefaultYearlyVacationDays"))
        vacation = _num(fields.get("CurrentVacationBalance"))
        if entitlement > 0 or vacation != 0:
            filed += bool(await record_finding(
                session, CASE_EMPLOYMENT_TYPE_SUSPECT, "staff", None, sp_id,
                "An hourly record has a vacation entitlement or vacation balance, which hourly staff should not carry.",
                {"salary_hourly": fields.get("SalaryHourly"),
                 "vacation_entitlement": entitlement, "vacation_balance": vacation},
                run_id,
            ))

    # 2. Username as name: the Title reads like a login, not a person's name.
    if _looks_like_username(name, email):
        filed += bool(await record_finding(
            session, CASE_USERNAME_AS_NAME, "staff", None, sp_id,
            "The name looks like a login rather than a person's name.",
            {"name": name, "email": email},
            run_id,
        ))

    # 3. No user link: the person has no Microsoft 365 link (TitleLink), so the
    # directory cannot tie them to an account.
    if not _lookup_id(fields.get("TitleLinkLookupId")):
        filed += bool(await record_finding(
            session, CASE_NO_USER_LINK, "staff", None, sp_id,
            "The record has no Microsoft 365 person link.",
            {"name": name},
            run_id,
        ))

    # 4. Below zero predates rules: today's rules let only the person's floor pot
    # go below zero (vacation for salaried, sick for hourly). A different pot in
    # the negative must predate those rules, so flag it for HR.
    floor = "sick" if is_hourly(fields) else "vacation"
    negatives = {
        pot: _num(fields.get(column))
        for pot, column in _POT_COLUMNS.items()
        if pot != floor and _num(fields.get(column)) < 0
    }
    if negatives:
        filed += bool(await record_finding(
            session, CASE_BELOW_ZERO_PREDATES_RULES, "staff", None, sp_id,
            "A balance that today's rules keep at or above zero is negative, so it predates the rules.",
            {"floor_pot": floor, "negative_pots": negatives},
            run_id,
        ))
    return filed


async def _relink_supervisors(session: AsyncSession) -> int:
    """Resolve each manager link to the manager's own staff row, by name.

    SharePoint names a manager only by display name in AllManagers, so the
    manager's staff row is found by matching that name to a Staff Title. Left
    null when the manager is not in the directory (an external approver). This is
    an internal link, not the request-submitter match, so a name match is right
    here; it is a second pass because every staff row must exist first.

    Args:
        session: The open session; links are updated, not committed.

    Returns:
        How many links were newly resolved to a staff row.
    """
    staff_by_name = {
        (row.name or "").strip().lower(): row.id
        for row in (await session.execute(select(Staff))).scalars().all()
        if (row.name or "").strip()
    }
    resolved = 0
    for link in (await session.execute(
        select(StaffSupervisor).where(StaffSupervisor.manager_staff_id.is_(None))
    )).scalars().all():
        target = staff_by_name.get((link.manager_name or "").strip().lower())
        if target is not None:
            link.manager_staff_id = target
            resolved += 1
    return resolved


async def staff_sync_step(session: AsyncSession, sync_run) -> dict:
    """Import or update every Staff Directory record, and run the #341 detectors.

    A SYNC_STEPS callable: takes the caller's session and the sync-run row, writes
    the people, and returns counts for ``SyncRun.counts``. It does not commit; the
    caller commits once so the whole run lands together. Safe to run again.

    Args:
        session: The caller's open session.
        sync_run: The ``SyncRun`` row for this run (or None in a test); its id is
            stamped on the findings.

    Returns:
        ``{"seen", "imported", "updated", "failed", "findings", "email_conflicts",
        "relinked", "dropped_columns"}`` and, when any, ``"dropped_column_names"``.
    """
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    run_id = getattr(sync_run, "id", None)
    counts = {
        "seen": 0, "imported": 0, "updated": 0, "failed": 0,
        "findings": 0, "email_conflicts": 0, "relinked": 0,
    }
    seen_emails: dict[str, str] = {}                                 # normalised email -> first sp id
    dropped_columns: set[str] = set()

    for item in items:
        sp_id = str(item.get("id"))
        fields = dict(item.get("fields", {}))
        counts["seen"] += 1

        # Keep the email on the first record that claims it; blank it on a later
        # duplicate so the unique index holds. The person is still imported.
        norm = _norm_email(fields.get("EmailAddress"))
        if norm and seen_emails.get(norm, sp_id) != sp_id:
            fields["EmailAddress"] = ""                              # -> NULL, no collision
            counts["email_conflicts"] += 1
            logger.warning("Staff import: email %s on #%s duplicates #%s; kept on the first",
                           norm, sp_id, seen_emails[norm])
        elif norm:
            seen_emails[norm] = sp_id

        try:
            async with session.begin_nested():                      # one row cannot poison the batch
                row = (await session.execute(
                    staff_query().where(Staff.sp_item_id == sp_id)
                )).scalars().first()
                created = row is None
                if created:
                    row = Staff(sp_item_id=sp_id)
                    session.add(row)
                dropped = await apply_sp_fields(session, row, fields)
        except Exception as exc:                                    # record it, keep going
            counts["failed"] += 1
            logger.exception("Staff import failed for #%s: %s", sp_id, exc)
            continue

        counts["imported" if created else "updated"] += 1
        if dropped:
            dropped_columns.update(dropped)
        counts["findings"] += await _run_detectors(session, sp_id, fields, run_id)

    counts["relinked"] = await _relink_supervisors(session)
    counts["dropped_columns"] = len(dropped_columns)
    if dropped_columns:
        counts["dropped_column_names"] = sorted(dropped_columns)
    return counts
