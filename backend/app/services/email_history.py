"""Rebuild what the system emailed one person from the records it already keeps.

The ``email_log`` table only started filling on 2026-09-02. Before that the
server kept no send log, but every email it sends is written next to a record
that survives:

- The dispatcher writes a ``request_approval_state`` row when it builds the
  approval email for a request. That row proves the managers were emailed
  (``last_emailed_at`` is the latest send) and that the submitter's
  "Request Received" confirmation followed in the same run.
- Every approval and rejection claims a ``processing_log`` row before it
  sends the decision emails, so ``processed_at`` dates them exactly.
- Each dashboard-link renewal claims a ``processing_log`` row per person per
  day, and every email carrying a dashboard link updates
  ``dashboard_link_state``.
- Each Staff Directory nudge updates ``staff_setup_nudge`` for its recipient.
- The request items themselves (SharePoint) say who submitted them and, via
  the Staff Directory's ``AllManagers``, who was asked to approve them.

This module reads those records back and turns them into one dated timeline
per person: what was sent, to whom, when, and which record proves it. It is
read-only and degrades piecewise: a source that cannot be read is reported in
``notes`` rather than failing the lookup.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models import (
    DashboardLinkState,
    ProcessingLog,
    RequestApprovalState,
    StaffSetupNudge,
)
from app.services.email_log import normalize_address
from app.tasks.dashboard_links import CLAIM_NAMESPACE as DASHBOARD_RENEWAL_NAMESPACE

logger = logging.getLogger(__name__)

# (request kind, SharePoint list id) for the three request lists.
REQUEST_LISTS = (
    ("leave", settings.SP_LIST_LEAVE_REQUESTS),
    ("overtime", settings.SP_LIST_OVERTIME_REQUESTS),
    ("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT),
)

# Emails that leave no dated record and therefore cannot be listed.
UNLISTED_KINDS = (
    "auto-rejections at intake, 'Cannot Be Approved' notices, refunds, and "
    "jury duty / bereavement alerts leave no dated record and are not listed"
)


def _aware(moment: datetime | None) -> datetime | None:
    """Make a stored timestamp comparable: naive values are UTC by convention.

    Args:
        moment: A datetime from any of the tables (some columns are naive UTC,
            some timezone-aware), or None.

    Returns:
        The same instant as an aware UTC datetime, or None.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)   # naive columns store UTC
    return moment.astimezone(timezone.utc)


def _parse_sp_timestamp(value) -> datetime | None:
    """Parse a SharePoint ISO timestamp such as ``2026-08-27T11:32:06Z``.

    Args:
        value: The raw field value; None or a non-string yields None.

    Returns:
        An aware UTC datetime, or None when unparseable.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _sp_person_name(fields: dict, prefix: str, sp_user_to_name: dict) -> str:
    """Resolve a SharePoint Person field (``<prefix>LookupId``) to a display name.

    Args:
        fields: The list item's fields.
        prefix: Column name without the ``LookupId`` suffix.
        sp_user_to_name: SharePoint user id to display name map.

    Returns:
        The display name, or "" when unknown.
    """
    lookup_id = fields.get(f"{prefix}LookupId")
    if not lookup_id:
        return ""
    try:
        return sp_user_to_name.get(int(lookup_id), "")
    except (TypeError, ValueError):
        return ""


def _submitter_name(kind: str, fields: dict, sp_user_to_name: dict, staff_by_id: dict) -> str:
    """Who submitted a request, by display name, for any of the three lists.

    Args:
        kind: "leave" | "overtime" | "carryover-payout".
        fields: The list item's fields.
        sp_user_to_name: SharePoint user id to display name map.
        staff_by_id: Staff Directory items keyed by item id (carryover uses it).

    Returns:
        The submitter's display name, or "".
    """
    if kind == "leave":
        return _sp_person_name(fields, "SubmittedTest", sp_user_to_name)
    if kind == "overtime":
        return _sp_person_name(fields, "SubmittedBy", sp_user_to_name)
    try:
        staff = staff_by_id.get(int(fields.get("EmployeeID")))
    except (TypeError, ValueError):
        staff = None
    return staff["fields"].get("Title", "") if staff else ""


def _intake_subject(kind: str, submitter: str, fields: dict) -> str:
    """Subject of the confirmation the submitter gets when a request is dispatched."""
    if kind == "leave":
        return f"Leave Request Received - {submitter}"
    if kind == "overtime":
        return f"Time Make-Up Request Received - {submitter}"
    return (
        "Request Received for Carry Over"
        if fields.get("TypeofRequest") == "Carry Over"
        else "Request Received for Payout"
    )


def _manager_subject(kind: str, submitter: str, fields: dict, item_id: str) -> str:
    """Subject of the approval request the managers get."""
    if kind == "leave":
        return f"Leave Request - {submitter}"
    if kind == "overtime":
        return f"Overtime Request - {submitter}"
    return f"{fields.get('TypeofRequest', '')} Request #{item_id} Submitted by {submitter}"


def _decision_subjects(kind: str, action: str, submitter: str, fields: dict, item_id: str) -> list[tuple[str, str | None]]:
    """Subjects sent when a request is approved or rejected.

    Returns:
        (subject, also_to) pairs; ``also_to`` names who else was copied.
    """
    if kind == "leave":
        if action == "approve":
            return [
                (f"{submitter} - Leave Request: Approved", None),
                (f"Updated Leave Balance - {submitter}", "the approving manager"),
            ]
        return [(f"{submitter} - Leave Request: Rejected", None)]
    if kind == "overtime":
        start = fields.get("StartDate", "")
        if action == "approve":
            return [(f"{submitter} Overtime Approved - {start}", "the approving manager")]
        return [(f"{submitter} Overtime Rejected - {start}", "the approving manager")]
    request_type = fields.get("TypeofRequest", "")
    word = "Approved" if action == "approve" else "Rejected"
    return [(f"{request_type} Request #{item_id} {word}", None)]


def _event(
    *,
    date: datetime | None,
    precision: str,
    subject: str,
    to: str,
    source: str,
    also_to: str | None = None,
    request_type: str | None = None,
    request_id: str | None = None,
    note: str | None = None,
) -> dict:
    """One timeline entry, in the shape the admin endpoint returns."""
    return {
        "date": date.isoformat() if date else None,
        # "exact": the record is the send itself; "approximate": the send
        # followed the record within the same run; "latest_only": the record
        # keeps only the most recent of possibly several sends.
        "date_precision": precision,
        "subject": subject,
        "to": [to],
        "also_to": also_to,
        "source": source,
        "request_type": request_type,
        "request_id": request_id,
        "note": note,
    }


async def _staff_context(employee_name: str | None, notes: list[str]) -> tuple[dict, dict, set[str]]:
    """Staff Directory lookups needed to place a person on other people's requests.

    Args:
        employee_name: The person's Staff Directory Title; None skips the read.
        notes: Collects a message if the directory cannot be read.

    Returns:
        (sp_user_to_name, staff_by_id, managed) where ``managed`` is the
        lower-cased names of everyone who lists this person in AllManagers.
    """
    if not employee_name:
        return {}, {}, set()
    try:
        # Lazy import: the route layer already owns these lookups, and the
        # services package must not import routes at module load.
        from app.routes.dashboard import _build_staff_lookups
        _by_name, staff_by_id, sp_user_to_name, mgr_to_emp = await _build_staff_lookups()
    except Exception:
        logger.exception("Email history: Staff Directory read failed")
        notes.append("Staff Directory could not be read; requests this person manages were not checked.")
        return {}, {}, set()
    managed = {n.strip().lower() for n in mgr_to_emp.get(employee_name.strip(), set())}
    return sp_user_to_name, staff_by_id, managed


async def reconstruct_email_history(
    *,
    employee_id: str | int | None,
    employee_name: str | None,
    address: str | None,
    since: datetime,
) -> dict:
    """Dated list of the emails this person was sent, rebuilt from stored records.

    Args:
        employee_id: Staff Directory item id (matches carryover ``EmployeeID``,
            dashboard-link records, and the renewal claims).
        employee_name: Staff Directory Title (matches request submitters and
            the AllManagers lists). None when the directory lookup failed.
        address: The person's email, normalised; used for setup-nudge rows and
            shown as the recipient on every event.
        since: Start of the window; events before it are dropped.

    Returns:
        ``events`` (newest first), ``notes`` (what could not be checked or is
        not recorded), and ``latest_dashboard_link_email_at``.
    """
    notes: list[str] = [UNLISTED_KINDS]
    events: list[dict] = []
    emp_id = str(employee_id) if employee_id else None
    name_l = (employee_name or "").strip().lower()
    to = address or "(address unknown)"

    sp_user_to_name, staff_by_id, managed = await _staff_context(employee_name, notes)

    # --- Requests this person submitted, or was asked to approve ---
    involved: list[tuple[str, str, str, dict, str, str]] = []  # kind, list, item, fields, role, submitter
    for kind, list_id in REQUEST_LISTS:
        try:
            items = await sp_client.get_list_items(list_id)      # not indexed: fetch all, filter here
        except Exception:
            logger.exception("Email history: %s list read failed", kind)
            notes.append(f"{kind} requests could not be read.")
            continue
        for item in items:
            fields = item.get("fields", {}) or {}
            item_id = str(item.get("id", ""))
            submitter = _submitter_name(kind, fields, sp_user_to_name, staff_by_id)
            role = None
            if kind == "carryover-payout":
                if emp_id and str(fields.get("EmployeeID")) == emp_id:
                    role = "employee"
            elif name_l and submitter.strip().lower() == name_l:
                role = "employee"
            if role is None and submitter.strip().lower() in managed:
                role = "manager"
            if role:
                involved.append((kind, list_id, item_id, fields, role, submitter))

    # --- The records written alongside each send ---
    list_ids = [list_id for _, list_id in REQUEST_LISTS]
    try:
        async with async_session() as session:
            states = {
                (row.list_id, row.item_id): row
                for row in (await session.execute(
                    select(RequestApprovalState).where(RequestApprovalState.list_id.in_(list_ids))
                )).scalars()
            }
            claims = list((await session.execute(
                select(ProcessingLog).where(
                    ProcessingLog.list_id.in_(list_ids + [DASHBOARD_RENEWAL_NAMESPACE])
                )
            )).scalars())
            link = await session.get(DashboardLinkState, emp_id) if emp_id else None
            nudges = [
                row for row in (await session.execute(select(StaffSetupNudge))).scalars()
                if address and normalize_address(row.recipient) == address
            ]
    except Exception:
        logger.exception("Email history: database read failed")
        notes.append("Send records in the database could not be read.")
        states, claims, link, nudges = {}, [], None, []

    decisions: dict[tuple[str, str], list[ProcessingLog]] = {}
    renewals: list[ProcessingLog] = []
    for claim in claims:
        if claim.list_id == DASHBOARD_RENEWAL_NAMESPACE:
            if claim.item_id == emp_id:
                renewals.append(claim)
        elif claim.action in ("approve", "reject"):
            decisions.setdefault((claim.list_id, claim.item_id), []).append(claim)

    def add(**kwargs):
        """Append an event when it falls inside the window."""
        date = kwargs.get("date")
        if date is not None and date >= since:
            events.append(_event(**kwargs))

    for kind, list_id, item_id, fields, role, submitter in involved:
        state = states.get((list_id, item_id))
        if role == "employee":
            if state is not None:
                # The confirmation is sent in the same dispatch that wrote the
                # approval-state row; the item's creation time dates that run.
                add(
                    date=_parse_sp_timestamp(fields.get("Created")),
                    precision="approximate",
                    subject=_intake_subject(kind, submitter, fields),
                    to=to,
                    source="sharepoint Created + request_approval_state",
                    request_type=kind,
                    request_id=item_id,
                )
            for claim in decisions.get((list_id, item_id), []):
                for subject, also_to in _decision_subjects(kind, claim.action, submitter, fields, item_id):
                    add(
                        date=_aware(claim.processed_at),
                        precision="exact",
                        subject=subject,
                        to=to,
                        also_to=also_to,
                        source=f"processing_log {claim.action}",
                        request_type=kind,
                        request_id=item_id,
                    )
        else:  # manager of the submitter
            if state is not None:
                reminders = state.reminder_count or 0
                add(
                    date=_aware(state.last_emailed_at),
                    precision="latest_only",
                    subject=("Reminder: " if reminders else "") + _manager_subject(kind, submitter, fields, item_id),
                    to=to,
                    also_to=f"every other manager of {submitter}",
                    source="request_approval_state.last_emailed_at",
                    request_type=kind,
                    request_id=item_id,
                    note=(
                        f"{reminders} earlier send(s) of this approval request; only the latest date is kept"
                        if reminders else None
                    ),
                )
            for claim in decisions.get((list_id, item_id), []):
                for subject, also_to in _decision_subjects(kind, claim.action, submitter, fields, item_id):
                    if also_to is None:
                        continue                                  # employee-only email
                    add(
                        date=_aware(claim.processed_at),
                        precision="exact",
                        subject=subject,
                        to=to,
                        also_to=submitter,
                        source=f"processing_log {claim.action}",
                        request_type=kind,
                        request_id=item_id,
                        note="copied to the manager who approved; only if that was this person",
                    )

    # --- Dashboard link renewals: one claim per person per day ---
    for claim in renewals:
        add(
            date=_aware(claim.processed_at),
            precision="exact",
            subject="Your Dashboard Link",
            to=to,
            source="processing_log dashboard-link-renewal",
        )

    # --- Staff Directory nudges to this person as a record creator ---
    for nudge in nudges:
        record = staff_by_id.get(int(nudge.employee_id)) if nudge.employee_id.isdigit() else None
        record_name = record["fields"].get("Title", "") if record else f"record #{nudge.employee_id}"
        subject = f"Staff Directory record needs attention - {record_name}"
        add(
            date=_aware(nudge.last_sent_at),
            precision="exact",
            subject=subject,
            to=to,
            source="staff_setup_nudge.last_sent_at",
            note=f"{nudge.send_count} send(s) in this run" if getattr(nudge, "send_count", None) else None,
        )
        if nudge.first_sent_at and _aware(nudge.first_sent_at) != _aware(nudge.last_sent_at):
            add(
                date=_aware(nudge.first_sent_at),
                precision="exact",
                subject=subject,
                to=to,
                source="staff_setup_nudge.first_sent_at",
            )

    events.sort(key=lambda e: e["date"] or "", reverse=True)
    notes.append("Reminder re-sends to managers keep only a count and the latest date.")
    return {
        "events": events,
        "notes": notes,
        "latest_dashboard_link_email_at": (
            _aware(link.last_sent_at).isoformat() if link and link.last_sent_at else None
        ),
    }
