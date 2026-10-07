"""Requests from people not in the Staff Directory yet: hold, remind, release.

On the public request page, someone who proves they own an email address that
is on no staff record (a new hire, typically) picks their supervisor and fills
in a form. Nothing can route that request yet (no balances, no manager), so:

1. ``hold_request`` keeps it in the held_requests table and emails the
   supervisor a link to their dashboard's Add Employee tab, prefilled with the
   person's name, email and location.
2. ``remind_held_requests`` (hourly, from tasks/held_request_reminders.py)
   emails the supervisor once more after 2 business days and the admins after 5.
3. ``release_held_requests`` runs when Add Employee creates a record with that
   email: each held form is submitted as a normal request, and the person is
   told it went through.

Emails go through send_email, so they move with it from one email service to
the next with no change here.
"""

import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.graph.sharepoint import sp_client
from app.models import HeldRequest
from app.models.mixins import utcnow
from app.services.dashboard_tokens import generate_dashboard_url
from app.services.employee import (
    ADMIN_NAMES,
    LOCATION_PROVINCE_MAP,
    get_employee_by_email,
    get_employee_by_name,
)
from app.services.request_intake import parse_request_form, submit_request
from app.services.request_submitter import SOURCE_REQUEST_PAGE

logger = logging.getLogger(__name__)

TORONTO = ZoneInfo("America/Toronto")
SUPERVISOR_REMINDER_BUSINESS_DAYS = 2   # supervisor gets one more email after this
ADMIN_ESCALATION_BUSINESS_DAYS = 5      # then the admins are told
MAX_OPEN_PER_EMAIL = 5                  # held requests one address can have waiting
OPEN_STATUSES = ("held", "failed")      # still waiting to become a real request

REQUEST_LABELS = {
    "leave": "leave request",
    "overtime": "overtime entry",
    "carryover-payout": "carry-over or payout request",
}


class HoldError(ValueError):
    """A held request that cannot be accepted; the message is for the person."""


# ----- pure helpers -----

def business_days_between(start: date, end: date) -> int:
    """Count Monday-to-Friday days after ``start`` up to and including ``end``.

    Holidays are ignored on purpose: a reminder a day early does no harm.

    Args:
        start: The day the request was held.
        end: Today.

    Returns:
        0 on the same day; 1 the next weekday; weekends skipped.
    """
    days = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:                              # Monday=0 .. Friday=4
            days += 1
    return days


def toronto_date(moment: datetime) -> date:
    """The calendar date in Toronto of a stored UTC timestamp.

    Args:
        moment: Aware, or naive UTC (SQLite hands those back).

    Returns:
        The Toronto date.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(TORONTO).date()


def add_employee_url(supervisor_id: str, name: str, email: str, location: str) -> str:
    """The supervisor's dashboard link that opens Add Employee prefilled.

    Args:
        supervisor_id: Staff Directory id of the supervisor.
        name: Person's name as they typed it.
        email: Their verified address.
        location: Their location.

    Returns:
        A signed manager dashboard URL with add_* prefill parameters.
    """
    prefill = urlencode({"add_name": name, "add_email": email, "add_location": location})
    return f"{generate_dashboard_url('manager', supervisor_id)}&{prefill}"


def describe_form(request_type: str, form_data: dict) -> str:
    """One readable line saying what was asked for.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        form_data: The stored, checked form.

    Returns:
        For example "Vacation, 2026-11-02 to 2026-11-04".
    """
    if request_type == "leave":
        if form_data.get("partial_hours"):
            return f"{form_data['leave_type']}, {form_data['partial_hours']} hours on {form_data['start_date']}"
        return f"{form_data['leave_type']}, {form_data['start_date']} to {form_data['end_date']}"
    if request_type == "overtime":
        return f"Overtime, {form_data['hours']} hours on {form_data['date']}: {form_data['description']}"
    return f"{form_data['type_of_request']}, {form_data['days']} days"


# ----- the supervisor list -----

async def list_supervisors() -> list[dict]:
    """Everyone named as somebody's supervisor in the Staff Directory.

    Returns:
        [{"id", "name", "location"}] sorted by name, the people a new hire
        can pick from.
    """
    staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    by_name = {
        (s.get("fields", {}).get("Title") or "").strip().lower(): s for s in staff
    }
    names: set[str] = set()
    for s in staff:                                            # AllManagers holds the supervisors
        for entry in s.get("fields", {}).get("AllManagers") or []:
            if isinstance(entry, dict) and entry.get("LookupValue"):
                names.add(entry["LookupValue"].strip().lower())
    supervisors = []
    for name in names:
        record = by_name.get(name)
        if record:                                             # only those with a staff record
            f = record.get("fields", {})
            supervisors.append({"id": str(record["id"]), "name": f.get("Title", ""),
                                "location": f.get("Location", "")})
    return sorted(supervisors, key=lambda s: s["name"].lower())


# ----- holding -----

async def hold_request(
    email: str, name: str, location: str, supervisor_id: str, request_type: str, form: dict,
) -> HeldRequest:
    """Keep a request from someone not on staff, and email their supervisor.

    Args:
        email: Verified, normalised address.
        name: Their name as typed.
        location: One of the Staff Directory locations.
        supervisor_id: Staff Directory id from list_supervisors.
        request_type: "leave", "overtime" or "carryover-payout".
        form: The form body.

    Returns:
        The stored row.

    Raises:
        HoldError: On a bad name, location, supervisor or too many waiting.
        RequestFormError: On a bad form.
    """
    name = " ".join((name or "").split())                      # collapse stray spaces
    if len(name.split(" ")) < 2:
        raise HoldError("Enter your first and last name.")
    if location not in LOCATION_PROVINCE_MAP:
        raise HoldError("Choose your location from the list.")
    checked = parse_request_form(request_type, form)           # same checks as for staff

    supervisor = next((s for s in await list_supervisors() if s["id"] == str(supervisor_id)), None)
    if supervisor is None:
        raise HoldError("Choose your supervisor from the list.")
    supervisor_record = await get_employee_by_name(supervisor["name"])
    supervisor_email = (supervisor_record or {}).get("fields", {}).get("EmailAddress", "")
    if not supervisor_email:
        raise HoldError("That supervisor has no email on file. Choose another, or ask HR.")

    async with async_session() as session:
        waiting = (await session.scalars(
            select(HeldRequest).where(HeldRequest.email == email, HeldRequest.status.in_(OPEN_STATUSES))
        )).all()
        if len(waiting) >= MAX_OPEN_PER_EMAIL:
            raise HoldError("You already have several requests waiting. Your supervisor has been told.")
        row = HeldRequest(
            email=email, name=name, location=location,
            supervisor_id=supervisor["id"], supervisor_name=supervisor["name"],
            supervisor_email=supervisor_email,
            request_type=request_type, form_data=checked.model_dump(mode="json"),
            status="held", created_at=utcnow(),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

    try:
        await _email_supervisor(row, reminder=False)
    except Exception:  # noqa: BLE001 - the request is kept; the reminder will retry
        logger.exception("Could not email the supervisor about held request #%s", row.id)
    logger.info("Held %s request #%s for a new hire", request_type, row.id)
    return row


def _render_supervisor_email(row: HeldRequest, reminder: bool) -> str:
    """HTML for the supervisor: who, what, and the Add Employee link."""
    link = add_employee_url(row.supervisor_id, row.name, row.email, row.location)
    lead = "Reminder: " if reminder else ""
    return (
        f"<p>{lead}{html.escape(row.name)} ({html.escape(row.email)}, {html.escape(row.location)}) "
        f"picked you as their supervisor and made a {REQUEST_LABELS[row.request_type]} "
        "on the UCSH Out of Office request page.</p>"
        f"<p><b>{html.escape(describe_form(row.request_type, row.form_data))}</b></p>"
        "<p>They are not in the Staff Directory yet, so the request is on hold. "
        "Add them and it is submitted to you automatically.</p>"
        f'<p><a href="{html.escape(link)}">Add {html.escape(row.name)}</a></p>'
        "<p>If you are not their supervisor, reply to HR so it can be redirected.</p>"
    )


async def _email_supervisor(row: HeldRequest, reminder: bool) -> None:
    """Send (or resend) the supervisor email for one held request."""
    subject = f"{'Reminder: ' if reminder else ''}Add {row.name} to the Staff Directory"
    await send_email(to=[row.supervisor_email], subject=subject,
                     html_body=_render_supervisor_email(row, reminder))


async def _admin_emails() -> list[str]:
    """Email addresses of the admins (ADMIN_NAMES), skipping any not found."""
    emails = []
    for name in sorted(ADMIN_NAMES):
        record = await get_employee_by_name(name)
        email = (record or {}).get("fields", {}).get("EmailAddress", "")
        if email:
            emails.append(email)
    return emails


async def _email_admins(row: HeldRequest) -> None:
    """Tell the admins a held request has waited 5 business days."""
    to = await _admin_emails()
    if not to:
        logger.warning("No admin email found for held request #%s", row.id)
        return
    body = (
        f"<p>{html.escape(row.name)} ({html.escape(row.email)}, {html.escape(row.location)}) "
        f"has had a {REQUEST_LABELS[row.request_type]} on hold since "
        f"{toronto_date(row.created_at).isoformat()}, waiting for "
        f"{html.escape(row.supervisor_name)} to add them to the Staff Directory.</p>"
        f"<p><b>{html.escape(describe_form(row.request_type, row.form_data))}</b></p>"
        "<p>Add them from the admin dashboard's Add Employee tab, or see Held Requests.</p>"
    )
    await send_email(to=to, subject=f"Held request waiting: {row.name}", html_body=body)


async def remind_held_requests(today: date | None = None) -> dict:
    """Send the due reminders for every held request. Safe to run any time.

    Args:
        today: Toronto date to measure from; defaults to now.

    Returns:
        {"supervisor": n, "admins": n} reminders sent.
    """
    today = today or datetime.now(TORONTO).date()
    sent = {"supervisor": 0, "admins": 0}
    async with async_session() as session:
        rows = (await session.scalars(
            select(HeldRequest).where(HeldRequest.status == "held")
        )).all()
        for row in rows:
            waited = business_days_between(toronto_date(row.created_at), today)
            try:
                if row.supervisor_reminded_at is None and waited >= SUPERVISOR_REMINDER_BUSINESS_DAYS:
                    await _email_supervisor(row, reminder=True)
                    row.supervisor_reminded_at = utcnow()      # once only
                    sent["supervisor"] += 1
                if row.admins_notified_at is None and waited >= ADMIN_ESCALATION_BUSINESS_DAYS:
                    await _email_admins(row)
                    row.admins_notified_at = utcnow()          # once only
                    sent["admins"] += 1
            except Exception:  # noqa: BLE001 - one bad send must not stop the sweep
                logger.exception("Held request reminder failed for #%s", row.id)
        await session.commit()
    return sent


# ----- releasing -----

@dataclass
class ReleaseResult:
    """What happened to one held request on release.

    Attributes:
        held_id: The held_requests row id.
        status: "released", "failed" or "waiting".
        detail: The SharePoint item id, or why it did not go through.
    """
    held_id: int
    status: str
    detail: str


async def release_held_requests(email: str, only_id: int | None = None) -> list[ReleaseResult]:
    """Submit the held requests for an address that is now on staff.

    Called after Add Employee creates a record, and by an admin's retry.

    Args:
        email: The new staff record's email.
        only_id: Release just this row (an admin retry); None for all.

    Returns:
        One ReleaseResult per open held request for the address.
    """
    email = (email or "").strip().lower()
    employee = await get_employee_by_email(email)
    if not employee:
        return []                                              # still not on staff

    # Without the SubmitterEmail column, a request only finds its person through
    # the site's user list. Leave it held rather than create an unroutable item.
    from app.services.leave_requests import _resolve_user_lookup_id
    waiting_for_site = (
        not settings.REQUEST_EMAIL_COLUMNS_ENABLED and not await _resolve_user_lookup_id(email)
    )

    results: list[ReleaseResult] = []
    async with async_session() as session:
        query = select(HeldRequest).where(
            HeldRequest.email == email, HeldRequest.status.in_(OPEN_STATUSES)
        )
        if only_id is not None:
            query = query.where(HeldRequest.id == only_id)
        rows = (await session.scalars(query.order_by(HeldRequest.id))).all()
        for row in rows:
            if waiting_for_site:
                row.last_error = "Waiting for them to be linked to the SharePoint site."
                results.append(ReleaseResult(row.id, "waiting", row.last_error))
                continue
            try:
                form = parse_request_form(row.request_type, row.form_data)
                item = await submit_request(row.request_type, form, employee, SOURCE_REQUEST_PAGE)
                row.status, row.released_at = "released", utcnow()
                row.sp_item_id, row.last_error = str(item.get("id")), None
                results.append(ReleaseResult(row.id, "released", row.sp_item_id))
            except Exception as e:  # noqa: BLE001 - a bad form or a SharePoint error; kept for an admin retry
                logger.exception("Could not release held request #%s", row.id)
                row.status, row.last_error = "failed", str(e)[:500]
                results.append(ReleaseResult(row.id, "failed", row.last_error))
        await session.commit()
        # Every row was open when read, so any now "released" went through in this run.
        released = [r for r in rows if r.status == "released"]

    if released:
        try:
            await _email_released(employee, released)
        except Exception:  # noqa: BLE001 - the requests went through either way
            logger.exception("Could not tell %s their held requests were submitted", email)
    return results


async def _email_released(employee: dict, rows: list[HeldRequest]) -> None:
    """Tell the person their held requests were submitted."""
    fields = employee.get("fields", {})
    items = "".join(
        f"<li>{html.escape(describe_form(r.request_type, r.form_data))}</li>" for r in rows
    )
    body = (
        f"<p>Hi {html.escape(fields.get('Title', ''))},</p>"
        "<p>You have been added to the Staff Directory, so the requests you made on the "
        "UCSH Out of Office request page have been sent to your manager:</p>"
        f"<ul>{items}</ul>"
        "<p>You will get an email when each one is approved or rejected.</p>"
    )
    await send_email(to=[fields.get("EmailAddress", "")], subject="Your requests were submitted",
                     html_body=body)


# ----- admin view -----

async def list_held_requests(include_closed: bool = False) -> list[dict]:
    """Held requests for the admin dashboard, newest first.

    Args:
        include_closed: Also return released and cancelled rows.

    Returns:
        Plain dicts, snake_case, with a readable "summary".
    """
    async with async_session() as session:
        query = select(HeldRequest).order_by(HeldRequest.id.desc())
        if not include_closed:
            query = query.where(HeldRequest.status.in_(OPEN_STATUSES))
        rows = (await session.scalars(query)).all()
    return [
        {
            "id": r.id, "email": r.email, "name": r.name, "location": r.location,
            "supervisor_name": r.supervisor_name, "request_type": r.request_type,
            "summary": describe_form(r.request_type, r.form_data), "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "supervisor_reminded_at": r.supervisor_reminded_at.isoformat() if r.supervisor_reminded_at else None,
            "admins_notified_at": r.admins_notified_at.isoformat() if r.admins_notified_at else None,
            "sp_item_id": r.sp_item_id, "last_error": r.last_error,
        }
        for r in rows
    ]


async def cancel_held_request(held_id: int) -> bool:
    """Mark one open held request cancelled (a mistake, or a duplicate).

    Args:
        held_id: The held_requests row id.

    Returns:
        True when an open row was cancelled.
    """
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        if row is None or row.status not in OPEN_STATUSES:
            return False
        row.status = "cancelled"
        await session.commit()
    return True


async def held_request_email(held_id: int) -> str | None:
    """The email on one held request, for an admin retry."""
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        return row.email if row else None
