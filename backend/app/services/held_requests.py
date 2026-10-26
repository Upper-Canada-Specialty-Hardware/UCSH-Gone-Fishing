"""Requests from people not in the Staff Directory yet: hold, remind, release.

On the public request page, someone who proves they own an email address that
is on no staff record (a new hire, typically) picks their supervisor and fills
in a form. Nothing can route that request yet (no balances, no manager), so:

1. ``hold_request`` keeps it in the held_requests table and emails the
   supervisor a link to their dashboard's Add Employee tab, prefilled with the
   person's name, email and department.
2. ``remind_held_requests`` (hourly, from tasks/held_request_reminders.py)
   emails the supervisor once more after 2 business days and the admins after 5.
3. ``release_held_requests`` runs when Add Employee creates a record with that
   email: each held form is submitted as a normal request, and the person is
   told it went through. While the SubmitterEmail column is off, a person not
   yet on the SharePoint site cannot be routed, so their rows wait as
   "waiting_site" and ``retry_waiting_site`` tries again every hour.

Statuses: held (not on staff yet), waiting_site (on staff, not on the site
yet), releasing (being submitted right now), failed (an admin can retry),
released and cancelled (closed).

Emails go through send_email, so they move with it from one email service to
the next with no change here.
"""

import hashlib
import hmac
import html
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.models import HeldRequest
from app.models.mixins import utcnow
from app.services.dashboard_tokens import generate_dashboard_url
from app.services.employee import (
    ADMIN_NAMES,
    get_employee_by_email,
    get_employee_by_id,
    get_employee_by_name,
)
from app.services.manager_match import lists_manager
from app.services.request_intake import ensure_request_type_open, parse_request_form, submit_request
from app.services.request_submitter import SOURCE_REQUEST_PAGE
from app.repositories.staff_store import staff_store  # Staff Directory reads follow staff_storage
from app.templates_render import render_held_supervisor_actions

logger = logging.getLogger(__name__)

TORONTO = ZoneInfo("America/Toronto")
SUPERVISOR_REMINDER_BUSINESS_DAYS = 2   # supervisor gets one more email after this
ADMIN_ESCALATION_BUSINESS_DAYS = 5      # then the admins are told
MAX_OPEN_PER_EMAIL = 1                  # one request until set up (#442)
RELEASE_SIGN_IN_DAYS = 7                # a released row may sign the new hire in for this long (#482)
OPEN_STATUSES = ("held", "waiting_site", "failed")   # waiting to become a real request (releasable)
# Every status in which the person still has a request in flight, so the request
# page shows its status instead of a new form and the submit endpoints refuse a
# new one (#442). "not_on_team" is in flight: the person has still to re-pick.
ACTIVE_STATUSES = ("held", "waiting_site", "failed", "releasing", "not_on_team")
# For someone already on staff only these still wait on a supervisor. A row that
# is failed, waiting_site or releasing is the system's to finish (an admin
# retries it), so it must not stop them making new requests.
SUPERVISOR_STATUSES = ("held", "not_on_team")
# What an admin may cancel: anything not being submitted right now. A row in
# "releasing" is mid-submit; cancelling it would be overwritten by the release.
CANCELLABLE_STATUSES = ("held", "waiting_site", "failed", "not_on_team")

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


def _as_utc(moment: datetime) -> datetime:
    """A stored timestamp as aware UTC (SQLite hands naive values back).

    Args:
        moment: Aware, or naive UTC.

    Returns:
        The same instant, tagged UTC when it was naive, so arithmetic is safe.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=ZoneInfo("UTC"))


def add_employee_url(supervisor_id: str, name: str, email: str, department: str, held_id: int | None = None) -> str:
    """The supervisor's dashboard link that opens Add Employee prefilled.

    Args:
        supervisor_id: Staff Directory id of the supervisor.
        name: Person's name as they typed it.
        email: Their verified address.
        department: The department they picked.
        held_id: The held request, so the page can say what happened to it and
            offer a profile edit when the name is already on staff (#444, #462).

    Returns:
        A signed manager dashboard URL with add_* prefill parameters.
    """
    query = {"add_name": name, "add_email": email, "add_department": department}
    if held_id is not None:
        query["add_held"] = str(held_id)                     # which held request the link is for
    prefill = urlencode(query)
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


# ----- the "Not on my team" link (#443) -----

def not_on_team_token(held_id: int, supervisor_id: str) -> str:
    """A signed token for the "Not on my team" link in one supervisor's email.

    The supervisor id is part of what is signed, so a stale link from a previous
    supervisor cannot decline the request once it has moved on to someone else.

    Args:
        held_id: The held request the link is for.
        supervisor_id: The supervisor the email was sent to.

    Returns:
        ``"<held_id>.<supervisor_id>.<signature>"``.
    """
    message = f"held-not-on-team:{held_id}:{supervisor_id}".encode()   # both ids, kept apart by prefix
    signature = hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    return f"{held_id}.{supervisor_id}.{signature}"


def decode_not_on_team_token(token: str) -> tuple[int, str] | None:
    """The ``(held_id, supervisor_id)`` a not-on-team token names, when valid.

    Args:
        token: A token from ``not_on_team_token``.

    Returns:
        The pair, or None for a malformed or forged token.
    """
    parts = (token or "").split(".")                           # "<held>.<supervisor>.<sig>"
    if len(parts) != 3 or not parts[0].isdigit():
        return None                                            # not a token at all
    held_id, supervisor_id, _ = parts
    expected = not_on_team_token(int(held_id), supervisor_id)   # re-sign and compare in constant time
    return (int(held_id), supervisor_id) if hmac.compare_digest(token, expected) else None


def not_on_team_link(held_id: int, supervisor_id: str) -> str:
    """The full "Not on my team" URL, on the public site (#443, #483).

    Points at the public request site's hash route (the same base the emailed
    dashboard links use), not the backend, so the supervisor lands on a styled
    page like the other request-page screens. That page reads the request over
    the JSON endpoints and shows a confirm button; the signed backend GET/POST
    HTML routes stay unchanged, so links in emails already sent keep working.

    Args:
        held_id: The held request the link is for.
        supervisor_id: The supervisor the email was sent to.

    Returns:
        A signed URL on ``DASHBOARD_FRONTEND_URL`` (the public site).
    """
    base = settings.DASHBOARD_FRONTEND_URL.rstrip("/")         # the public site, same base as the dashboard links
    return f"{base}/#/held/not-on-team?token={not_on_team_token(held_id, supervisor_id)}"


# ----- one email thread per held request per recipient (#443) -----

def _thread_domain() -> str:
    """The domain used in generated Message-IDs, taken from the sending address.

    Returns:
        The part after "@" in ``SENDER_EMAIL``, or a safe placeholder when unset.
    """
    _, _, domain = (settings.SENDER_EMAIL or "").partition("@")
    return domain.strip().lower() or "ucshoutofoffice.local"


def thread_root_id(held_id: int, recipient: str) -> str:
    """A stable conversation root id per held request and recipient (#443).

    Every email about one held request to one recipient references this same id,
    so a mail client groups them as one conversation. It is derived, never a real
    message's id, so no two sent messages ever share a Message-ID.

    Args:
        held_id: The held request.
        recipient: The address the thread belongs to.

    Returns:
        An angle-bracketed Message-ID string, stable for this pair.
    """
    raw = f"held-thread:{held_id}:{(recipient or '').strip().lower()}"   # per request + recipient
    digest = hmac.new(settings.APPROVAL_LINK_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:24]
    return f"<held-{held_id}-{digest}@{_thread_domain()}>"


def thread_headers(held_id: int, recipient: str) -> list[dict]:
    """Message-ID / In-Reply-To / References so emails to one recipient thread (#443).

    Each email gets its own unique Message-ID and points In-Reply-To and
    References at the conversation's stable root, so the mail client files every
    email about one request under one conversation, however many supervisors it
    passes through.

    Args:
        held_id: The held request the email is about.
        recipient: The address this email is going to.

    Returns:
        A list of ``{"header", "value"}`` for ``send_email``'s ``headers``.
    """
    root = thread_root_id(held_id, recipient)                  # the conversation's stable anchor
    unique = f"<{uuid.uuid4().hex}@{_thread_domain()}>"        # this one message's own id
    return [
        {"header": "Message-ID", "value": unique},
        {"header": "In-Reply-To", "value": root},
        {"header": "References", "value": root},
    ]


def _supervisor_subject(row: HeldRequest) -> str:
    """The stable subject for the supervisor thread about one held request (#443).

    Args:
        row: The held request.

    Returns:
        A subject that does not change between the first email and the reminders,
        so they thread.
    """
    return f"Add {row.name} to the Staff Directory"


# The person's own thread keeps one stable subject across every update.
PERSON_SUBJECT = "Your UCSH Out of Office request"


# ----- departments and the supervisor search -----

MIN_SEARCH_LETTERS = 2      # typed letters before any name is shown
MAX_SEARCH_RESULTS = 8      # names shown per search


def _clean(text: str | None) -> str:
    """Trimmed, single-spaced text; "" for None."""
    return " ".join((text or "").split())


async def _staff() -> list[dict]:
    """Every Staff Directory record (the list is not indexed, so all of it)."""
    return await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)


async def list_departments() -> list[str]:
    """The departments a new starter can pick, as the Staff Directory spells them.

    Returns:
        Each distinct non-empty Department value once (case ignored), sorted.
    """
    seen: dict[str, str] = {}
    for record in await _staff():
        department = _clean(record.get("fields", {}).get("Department"))
        if department:
            seen.setdefault(department.lower(), department)    # first spelling wins
    return sorted(seen.values(), key=str.lower)


def _in_department(record: dict, department: str) -> bool:
    """Whether a staff record is in the department (case and spacing ignored)."""
    return _clean(record.get("fields", {}).get("Department")).lower() == _clean(department).lower()


async def search_department(department: str, query: str) -> list[dict]:
    """People in a department whose name contains the typed text.

    Anyone in the department can be picked, not only people who supervise
    someone already: a newly made supervisor has nobody under them yet.

    Args:
        department: One of list_departments.
        query: What the new starter typed.

    Returns:
        Up to MAX_SEARCH_RESULTS [{"id", "name"}] sorted by name; none until
        MIN_SEARCH_LETTERS letters are typed.
    """
    query = _clean(query).lower()
    if len(query) < MIN_SEARCH_LETTERS:
        return []
    matches = [
        {"id": str(r["id"]), "name": _clean(r.get("fields", {}).get("Title"))}
        for r in await _staff()
        if _in_department(r, department) and query in _clean(r.get("fields", {}).get("Title")).lower()
    ]
    return sorted(matches, key=lambda m: m["name"].lower())[:MAX_SEARCH_RESULTS]


async def find_supervisor(department: str, supervisor_id: str | None, supervisor_email: str | None) -> dict:
    """The supervisor a new starter named, by search pick or by work email.

    Args:
        department: The department the new starter picked.
        supervisor_id: Staff Directory id from search_department, or None.
        supervisor_email: The work email typed when the search did not find
            them, or None. Anyone on staff, in any department.

    Returns:
        {"id", "name", "email"} of the supervisor.

    Raises:
        HoldError: When neither or both are given, the person is not found or
            not in the department, or has no email on file.
    """
    supervisor_id = _clean(supervisor_id)
    supervisor_email = _clean(supervisor_email).lower()
    if bool(supervisor_id) == bool(supervisor_email):
        raise HoldError("Pick your supervisor, or type their work email.")
    if supervisor_id:
        record = next((r for r in await _staff() if str(r["id"]) == supervisor_id), None)
        if record is None or not _in_department(record, department):
            raise HoldError("Pick your supervisor from the search.")
    else:
        record = await get_employee_by_email(supervisor_email)
        if record is None:
            raise HoldError("Nobody on staff has that work email. Check it, or ask HR.")
    fields = record.get("fields", {})
    email = _clean(fields.get("EmailAddress"))
    if not email:
        raise HoldError("That supervisor has no email on file. Pick another, or ask HR.")
    return {"id": str(record["id"]), "name": _clean(fields.get("Title")), "email": email}


# ----- holding -----

async def hold_request(
    email: str, name: str, department: str, supervisor_id: str | None, request_type: str, form: dict,
    *, supervisor_email: str | None = None,
) -> HeldRequest:
    """Keep a request from someone not on staff, and email their supervisor.

    Args:
        email: Verified, normalised address.
        name: Their name as typed.
        department: One of the Staff Directory departments.
        supervisor_id: Staff Directory id from search_department, or None.
        request_type: "leave", "overtime" or "carryover-payout".
        form: The form body.
        supervisor_email: The supervisor's work email, typed when the search
            did not find them; used instead of supervisor_id.

    Returns:
        The stored row.

    Raises:
        HoldError: On a bad name, department, supervisor or too many waiting.
        RequestFormError: On a bad form, or a carry-over/payout request
            while those are closed.
    """
    name = " ".join((name or "").split())                      # collapse stray spaces
    if len(name.split(" ")) < 2:
        raise HoldError("Enter your first and last name.")
    await ensure_request_type_open(request_type)               # carry-over/payout only in season, before anything else is checked
    # The Staff Directory's own spelling, so Add Employee is prefilled with it.
    department = next((d for d in await list_departments() if d.lower() == _clean(department).lower()), None)
    if department is None:
        raise HoldError("Choose your department from the list.")
    checked = parse_request_form(request_type, form)           # same checks as for staff
    supervisor = await find_supervisor(department, supervisor_id, supervisor_email)

    async with async_session() as session:
        waiting = (await session.scalars(
            select(HeldRequest).where(HeldRequest.email == email, HeldRequest.status.in_(ACTIVE_STATUSES))
        )).all()
        if len(waiting) >= MAX_OPEN_PER_EMAIL:                  # one request until set up (#442)
            raise HoldError(
                "You already have a request waiting to be set up. "
                "You can make more once your supervisor has added you."
            )
        row = HeldRequest(
            email=email, name=name, department=department,
            supervisor_id=supervisor["id"], supervisor_name=supervisor["name"],
            supervisor_email=supervisor["email"],
            # The supervisor history starts with the one they picked; re-picks append (#443).
            supervisors_involved=[{
                "id": supervisor["id"], "name": supervisor["name"],
                "email": supervisor["email"], "outcome": None,
            }],
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


async def open_held_request_for(*, email: str, statuses: tuple = ACTIVE_STATUSES) -> HeldRequest | None:
    """The person's one in-flight held request, if they have one (#442).

    Used by the gate: while a person has a request waiting to be set up, the
    request page shows its status and the submit endpoints refuse a new one. A new
    hire is found by their verified email.

    Args:
        email: The person's address to match on ``email``.
        statuses: Which statuses count; ``SUPERVISOR_STATUSES`` for someone
            already on staff, whose failed or stuck release is an admin's to fix.

    Returns:
        The newest matching held request for them, or None when they have none.
    """
    normalised = (email or "").strip().lower()
    if not normalised:                                         # nothing to match on
        return None
    async with async_session() as session:
        return await session.scalar(
            select(HeldRequest)
            .where(HeldRequest.email == normalised, HeldRequest.status.in_(statuses))
            .order_by(HeldRequest.id.desc()).limit(1)          # the newest one, if several ever existed
        )


def _where(row: HeldRequest) -> str:
    """Department, or the location older rows were held with."""
    return row.department or row.location or ""


def _render_supervisor_email(row: HeldRequest, reminder: bool) -> str:
    """HTML for the supervisor: who the new hire is, the request, then two buttons.

    The buttons (#483) are Add, which opens Add Employee filled in from this
    request, and Not on my team, which opens the signed public page that hands
    the request back (#443) so the person picks their supervisor again.

    Args:
        row: The held request; names the new hire and the current supervisor.
        reminder: True for a re-send, which opens with "Reminder: ".

    Returns:
        The email body HTML.
    """
    add_url = add_employee_url(row.supervisor_id, row.name, row.email, row.department or "", row.id)
    not_on_team_url = not_on_team_link(row.id, row.supervisor_id)   # signed for this supervisor only
    lead = "Reminder: " if reminder else ""
    return (
        f"<p>{lead}{html.escape(row.name)} ({html.escape(row.email)}, {html.escape(_where(row))}) "
        f"picked you as their supervisor and made a {REQUEST_LABELS[row.request_type]} "
        "on the UCSH Out of Office request page.</p>"
        f"<p><b>{html.escape(describe_form(row.request_type, row.form_data))}</b></p>"
        "<p>They are not in the Staff Directory yet, so the request is on hold. "
        "Add them and it is submitted to you automatically.</p>"
        # the template escapes the name and both links (autoescape)
        + render_held_supervisor_actions(row.name, add_url, not_on_team_url)
    )


async def _email_supervisor(row: HeldRequest, reminder: bool) -> None:
    """Send (or resend) the supervisor email for one held request, threaded (#443).

    The subject stays the same between the first email and every reminder or
    re-send, and the threading headers point at this supervisor's conversation
    root, so a mail client groups them as one thread.
    """
    await send_email(
        to=[row.supervisor_email], subject=_supervisor_subject(row),
        html_body=_render_supervisor_email(row, reminder),
        headers=thread_headers(row.id, row.supervisor_email),
    )


# ----- "Not on my team", picking another supervisor, telling earlier ones (#443) -----

async def _email_person(row: HeldRequest, lines: list[str]) -> None:
    """Send one threaded update to the person about their held request (#443).

    Args:
        row: The held request; the thread belongs to ``row.email``.
        lines: Paragraphs of the body, blanks skipped.
    """
    body = "".join(f"<p>{line}</p>" for line in lines if line)
    await send_email(
        to=[row.email], subject=PERSON_SUBJECT, html_body=body,
        headers=thread_headers(row.id, row.email),         # same thread as every other update to them
    )


def _mark_outcome(involved: list | None, supervisor_id: str, outcome: str) -> list:
    """Record a supervisor's outcome in the involved list, returning a new list.

    A new list of new dicts is returned (not an in-place edit) so SQLAlchemy sees
    the JSON column as changed and persists it.

    Args:
        involved: The current ``supervisors_involved`` list, or None.
        supervisor_id: Whose entry to mark.
        outcome: The outcome to set ("not_on_team" / "handled"), on their first
            still-open entry only.

    Returns:
        The updated list.
    """
    result = []
    for entry in (involved or []):
        copy = dict(entry)                                   # copy so the reassignment is a real change
        if str(copy.get("id")) == str(supervisor_id) and copy.get("outcome") is None:
            copy["outcome"] = outcome                        # mark their still-open turn
        result.append(copy)
    return result


async def _tell_decliner(row: HeldRequest, address: str) -> None:
    """Tell the supervisor who just handed the request back that it moved on (#443, #462).

    One threaded note, sent once, when the person picks someone else. It does not
    name the new supervisor: nothing about a request is shared between managers
    (Robert's ruling), and a supervisor hears nothing more once it has left them.

    Args:
        row: The held request, after the new supervisor is set.
        address: The email of the supervisor who declined it.
    """
    if not address or address.strip().lower() == (row.supervisor_email or "").strip().lower():
        return                                               # nobody to tell, or it came back to them
    line = (f"{html.escape(row.name)}'s request has moved to another supervisor. "
            "Nothing is needed from you.")                   # no name: no history between managers
    try:
        await send_email(
            to=[address], subject=_supervisor_subject(row), html_body=f"<p>{line}</p>",
            headers=thread_headers(row.id, address),         # their own thread about this request
        )
    except Exception:  # noqa: BLE001 - informational only
        logger.exception("Could not tell the decliner about held request #%s", row.id)


async def decline_held_request(held_id: int, supervisor_id: str) -> str:
    """A supervisor says the person is not on their team (#443).

    Moves the request to "not_on_team", records that this supervisor declined,
    and tells the person to pick another supervisor; the request itself does not
    change. Guarded so a stale link cannot act once the request has moved on.

    Args:
        held_id: The held request the link names.
        supervisor_id: The supervisor the link was issued to.

    Returns:
        "declined" when it was moved; "already" when this supervisor no longer
        holds it (already declined, moved on, or closed); "unknown" when there is
        no such row.
    """
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        if row is None:
            return "unknown"
        # Only the current supervisor, while they still hold it, may decline it.
        if row.status != "held" or str(row.supervisor_id) != str(supervisor_id):
            return "already"
        row.status = "not_on_team"                           # waiting on the person to pick again
        row.supervisors_involved = _mark_outcome(row.supervisors_involved, supervisor_id, "not_on_team")
        row.supervisor_reminded_at = None                    # no "add them" reminders while it waits
        await session.commit()
        await session.refresh(row)
    decliner = row.supervisor_name
    try:
        await _email_person(row, [
            f"{html.escape(decliner)} let us know you are not on their team.",
            "Open your request page and pick your supervisor again. Your request stays the same.",
        ])
    except Exception:  # noqa: BLE001 - the state change stands even if the email cannot go
        logger.exception("Could not tell the person their request was handed back (#%s)", held_id)
    return "declined"


def _readable_date(moment: datetime) -> str:
    """A stored timestamp as a Toronto date people read, e.g. "Nov 2, 2026"."""
    day = toronto_date(moment)
    return f"{day:%b} {day.day}, {day.year}"                  # no zero padding, on any platform


async def not_on_team_link_state(held_id: int, supervisor_id: str) -> dict | None:
    """What has happened to a held request, as seen by one supervisor's link (#462).

    The "Not on my team" page uses this so a link opened late says what really
    happened instead of a generic thanks. A supervisor who no longer holds the
    request only learns that it left them: nothing about where it went or how it
    ended is shared between managers.

    Args:
        held_id: The held request the signed link names.
        supervisor_id: The supervisor the link was issued to.

    Returns:
        None when there is no such row, else ``{"state", "name"}`` plus
        ``"set_up_on"`` and ``"request_number"`` for a released request. State is
        one of: "open" (they still hold it and may decline), "declined" (they
        already said not on their team, or it moved off them), "released",
        "cancelled" or "sending" (on staff now, request on its way).
    """
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
    if row is None:
        return None
    result = {"name": row.name}
    if str(row.supervisor_id) != str(supervisor_id) or row.status == "not_on_team":
        result["state"] = "declined"                         # it left them; say nothing more
    elif row.status == "released":
        result.update({
            "state": "released", "request_number": row.sp_item_id or "",
            "set_up_on": _readable_date(row.released_at) if row.released_at else "",
        })
    elif row.status == "cancelled":
        result["state"] = "cancelled"
    elif row.status == "held":
        result["state"] = "open"                             # still theirs: the button works
    else:                                                    # waiting_site, releasing, failed
        result["state"] = "sending"
    return result


async def not_on_team_page_info(held_id: int, supervisor_id: str) -> dict | None:
    """What the public "Not on my team" page shows for one supervisor's link (#483).

    Wraps ``not_on_team_link_state`` (so a link opened late says what really
    happened) and, only while the request is still theirs to decline, adds the
    request summary and the supervisor's name for the confirm screen. Once it has
    moved on, nothing about where it went or how it ended is shared (#462).

    Args:
        held_id: The held request the signed link names.
        supervisor_id: The supervisor the link was issued to.

    Returns:
        None when there is no such row, else ``{"state", "name",
        "supervisor_name", "summary", "request_number", "set_up_on"}``. State is
        the same set ``not_on_team_link_state`` returns ("open", "declined",
        "released", "cancelled", "sending"); ``supervisor_name`` and ``summary``
        are filled only when state is "open".
    """
    state = await not_on_team_link_state(held_id, supervisor_id)   # one read; the #462 privacy rules apply
    if state is None:
        return None                                          # no such row: the caller answers "invalid"
    result = {
        "state": state["state"], "name": state.get("name", ""),
        "supervisor_name": "", "summary": "",
        "request_number": state.get("request_number", ""), "set_up_on": state.get("set_up_on", ""),
    }
    if state["state"] == "open":                             # still theirs: show the request and who was asked
        row = await _load_held(held_id)
        if row is not None:
            result["supervisor_name"] = row.supervisor_name
            result["summary"] = describe_form(row.request_type, row.form_data)
    return result


async def _load_held(held_id: int) -> HeldRequest | None:
    """One held request row by id, or None (a small read used by the page info)."""
    async with async_session() as session:
        return await session.get(HeldRequest, held_id)


async def repick_supervisor(
    held_id: int, email: str, supervisor_id: str | None, supervisor_email: str | None,
) -> HeldRequest:
    """The person picks another supervisor after a "not on my team" (#443).

    Only the person who owns the request (matched by their verified email) may
    re-pick, only while it is in "not_on_team", and never the supervisor who just
    declined it. The request's dates and form do not change; it simply goes to
    the new supervisor, and the earlier ones are told it moved.

    Args:
        held_id: The held request to re-route.
        email: The verified address that owns the request.
        supervisor_id: Staff Directory id of the new supervisor, from the search.
        supervisor_email: The new supervisor's work email, typed instead.

    Returns:
        The updated held request.

    Raises:
        HoldError: When the request is not theirs, not waiting for a re-pick, the
            new supervisor is the one who declined, or cannot be found.
    """
    normalised = (email or "").strip().lower()
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        if row is None or row.email != normalised:           # not theirs, or gone
            raise HoldError("This request could not be found. Start again.")
        if row.status != "not_on_team":                      # nothing to re-pick
            raise HoldError("This request is not waiting for another supervisor.")
        declined_id = str(row.supervisor_id)                 # the one who said not their team
        declined_email = row.supervisor_email                # told once, below, that it moved on
        department = row.department or ""
        # Resolve the new supervisor within the same department (as the first pick was).
        supervisor = await find_supervisor(department, supervisor_id, supervisor_email)
        if str(supervisor["id"]) == declined_id:             # cannot pick the decliner again
            raise HoldError(f"{supervisor['name']} already said you are not on their team. Pick another.")
        row.supervisor_id = supervisor["id"]
        row.supervisor_name = supervisor["name"]
        row.supervisor_email = supervisor["email"]
        row.status = "held"                                  # back to waiting on the new supervisor
        row.supervisor_reminded_at = None                    # reminders count from this new ask
        involved = list(row.supervisors_involved or [])
        involved.append({                                    # a new turn for the new supervisor
            "id": supervisor["id"], "name": supervisor["name"],
            "email": supervisor["email"], "outcome": None,
        })
        row.supervisors_involved = involved
        await session.commit()
        await session.refresh(row)

    try:
        await _email_supervisor(row, reminder=False)         # first email in the new supervisor's thread
    except Exception:  # noqa: BLE001 - the re-pick stands; a reminder will retry
        logger.exception("Could not email the new supervisor for held request #%s", held_id)
    try:
        await _email_person(row, [
            f"Sent to {html.escape(row.supervisor_name)} instead. Nothing else changed."
        ])
    except Exception:  # noqa: BLE001
        logger.exception("Could not tell the person their request was re-routed (#%s)", held_id)
    await _tell_decliner(row, declined_email)                # only the one who just handed it back
    return row


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
        f"<p>{html.escape(row.name)} ({html.escape(row.email)}, {html.escape(_where(row))}) "
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
            # A new hire already added (by email) needs no "add them" reminder: the
            # release will submit their request (#445).
            if await get_employee_by_email(row.email):
                continue
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
        status: "released", "failed" or "waiting_site".
        detail: The SharePoint item id, or why it did not go through.
    """
    held_id: int
    status: str
    detail: str


async def release_held_requests(email: str, only_id: int | None = None) -> list[ReleaseResult]:
    """Submit the held requests for an address that is now on staff.

    Called after Add Employee creates a record, by an admin's retry, and
    hourly for rows waiting on site access. Each row is claimed with one
    conditional UPDATE before it is submitted, so two releases running at once
    (an admin retry during the hourly sweep, say) never submit it twice.

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

    # Without SubmitterEmail (requests still in SharePoint, columns not there), a
    # request only finds its person through the site's user list. Park it as
    # waiting_site rather than create an unroutable item. Never the case once
    # requests live in Postgres.
    from app.services.leave_requests import _resolve_user_lookup_id
    from app.services.request_submitter import requests_linked_by_email
    waiting_for_site = (
        not await requests_linked_by_email() and not await _resolve_user_lookup_id(email)
    )

    results: list[ReleaseResult] = []
    approver_ids: dict[int, str] = {}                          # held id -> the deciding manager's staff id
    async with async_session() as session:
        query = select(HeldRequest).where(
            HeldRequest.email == email, HeldRequest.status.in_(OPEN_STATUSES)
        )
        if only_id is not None:
            query = query.where(HeldRequest.id == only_id)
        rows = (await session.scalars(query.order_by(HeldRequest.id))).all()
        for row in rows:
            if waiting_for_site:
                # On staff but not routable yet; no "add them" reminders, retried hourly.
                row.status = "waiting_site"
                row.last_error = "Waiting for them to be linked to the SharePoint site."
                results.append(ReleaseResult(row.id, "waiting_site", row.last_error))
                continue
            claimed = await session.execute(                  # take the row; loses to a parallel release
                update(HeldRequest)
                .where(HeldRequest.id == row.id, HeldRequest.status.in_(OPEN_STATUSES))
                .values(status="releasing")
            )
            await session.commit()                            # the claim is visible to others now
            if claimed.rowcount != 1:
                continue                                       # another release has it
            row.status = "releasing"                           # keep the loaded copy in step
            try:
                form = parse_request_form(row.request_type, row.form_data)
                item = await submit_request(row.request_type, form, employee, SOURCE_REQUEST_PAGE)
                row.status, row.released_at = "released", utcnow()
                row.sp_item_id, row.last_error = str(item.get("id")), None
                # The current supervisor's turn is over: the person is set up.
                row.supervisors_involved = _mark_outcome(row.supervisors_involved, row.supervisor_id, "handled")
                approver_ids[row.id] = str((item.get("fields") or {}).get("ManagerID") or "")   # who decides it, when known
                results.append(ReleaseResult(row.id, "released", row.sp_item_id))
            except Exception as e:  # noqa: BLE001 - a bad form or a SharePoint error; kept for an admin retry
                logger.exception("Could not release held request #%s", row.id)
                row.status, row.last_error = "failed", str(e)[:500]
                results.append(ReleaseResult(row.id, "failed", row.last_error))
            await session.commit()                            # outcome saved row by row
        await session.commit()                                 # waiting_site changes
        # Only rows this run claimed can be "released" here; others were skipped.
        released = [r for r in rows if r.status == "released"]

    # Each released request closes its two conversations (#462): the person hears
    # it is in, and the supervisor who held it hears whether it now waits on them.
    # Earlier supervisors were told when it moved off them and hear nothing more.
    for row in released:
        try:
            await _email_released(employee, row, approver_ids.get(row.id, ""))
        except Exception:  # noqa: BLE001 - the request went through either way
            logger.exception("Could not send the release emails for held request #%s", row.id)
    return results


async def retry_waiting_site() -> int:
    """Try again to release rows waiting for their person to reach the site.

    Run hourly. Once the person opens the site (or the SubmitterEmail column is
    turned on), their requests go through without anyone pressing anything.

    Returns:
        How many rows were released this run.
    """
    async with async_session() as session:
        emails = (await session.scalars(
            select(HeldRequest.email).where(HeldRequest.status == "waiting_site").distinct()
        )).all()
    released = 0
    for email in emails:
        try:
            results = await release_held_requests(email)
            released += sum(1 for r in results if r.status == "released")
        except Exception:  # noqa: BLE001 - one address must not stop the others
            logger.exception("Retry of held requests waiting for site access failed")
    return released


async def _approver(employee: dict, row: HeldRequest, approver_id: str) -> tuple[bool, str]:
    """Whether the supervisor who held the request now decides it, and who does (#462).

    On Postgres the created request carries the deciding manager's staff id
    (``ManagerID``), which is exact. On SharePoint it does not, so the fallback is
    whether the person's record lists the supervisor among their managers.

    Args:
        employee: The person's new staff record.
        row: The released held request.
        approver_id: The created request's ``ManagerID``, or "" when unknown.

    Returns:
        ``(supervisor_decides, approver_name)``; the name is "" when unknown.
    """
    if approver_id:                                            # exact: the request names its manager
        if approver_id == str(row.supervisor_id):
            return True, row.supervisor_name
        try:
            manager = await get_employee_by_id(approver_id)    # their name for the person's email
        except Exception:  # noqa: BLE001 - the name is a nicety
            manager = None
        return False, _clean((manager or {}).get("fields", {}).get("Title"))
    # SharePoint: no manager id on the item, so read the record's manager list.
    decides = lists_manager(employee.get("fields", {}), row.supervisor_name)
    return decides, row.supervisor_name if decides else ""


async def _email_released(employee: dict, row: HeldRequest, approver_id: str) -> None:
    """Close both conversations about a released held request (#462).

    The person gets one more line in their own thread (set up, request number,
    who approves it), and the supervisor who held it gets one more line in theirs:
    it waits for their approval, or it went to the person's manager and nothing is
    needed from them. The approval and result emails that follow are threaded onto
    the same conversations by the outbox (see ``held_thread_for``).

    Args:
        employee: The person's new staff record.
        row: The released held request (``sp_item_id`` is the request number).
        approver_id: The created request's ``ManagerID``, or "" when unknown.
    """
    decides, approver = await _approver(employee, row, approver_id)
    who = html.escape(approver) if approver else "your manager"
    await _email_person(row, [
        f"You are set up in the Staff Directory. Your request is #{html.escape(row.sp_item_id or '')}, "
        f"with {who} to approve.",
        f"<b>{html.escape(describe_form(row.request_type, row.form_data))}</b>",   # which request, at a glance
        "You will get an email here when it is approved or rejected.",
    ])
    if decides:                                                # it now waits on the same person
        line = (f"{html.escape(row.name)} is set up. Their request #{html.escape(row.sp_item_id or '')} "
                "waits for your approval.")
    else:                                                      # routed to someone else: off their plate
        line = (f"{html.escape(row.name)} is set up. Their request went to their manager. "
                "Nothing is needed from you.")
    await send_email(
        to=[row.supervisor_email], subject=_supervisor_subject(row), html_body=f"<p>{line}</p>",
        headers=thread_headers(row.id, row.supervisor_email),   # the supervisor's thread about this request
    )


async def held_thread_for(session, request, recipient: str) -> tuple[str, list[dict]] | None:
    """The held-request conversation an outbox email belongs to, if any (#462).

    A request released from a held request keeps the two conversations it
    started: the person's and the supervisor's. When the outbox sends an email
    about that request (the manager's ask, a reminder, the result) to either of
    those two addresses, it is sent with that conversation's subject and threading
    headers so it lands in the same thread. Anyone else gets the ordinary email.

    Args:
        session: An open session (the outbox worker's).
        request: The ``RequestRecord`` the email is about.
        recipient: The address the email goes to.

    Returns:
        ``(subject, headers)`` for the thread, or None when the request did not
        come from a held request or the recipient is not one of its two people.
    """
    kind = {"carryover_payout": "carryover-payout"}.get(request.kind, request.kind)   # request_type spelling
    held = await session.scalar(
        select(HeldRequest).where(
            HeldRequest.sp_item_id == str(request.item_id), HeldRequest.request_type == kind,
            HeldRequest.status == "released",
        ).limit(1)
    )
    if held is None and getattr(request, "employee_email", None):
        # The release creates the request (which wakes the outbox) before it saves
        # the request number on the held row, so the first ask can arrive while the
        # row is still "releasing". Match that row by the person and the type.
        held = await session.scalar(
            select(HeldRequest).where(
                HeldRequest.email == request.employee_email.strip().lower(),
                HeldRequest.request_type == kind, HeldRequest.status == "releasing",
            ).order_by(HeldRequest.id.desc()).limit(1)
        )
    if held is None:                                           # an ordinary request
        return None
    address = (recipient or "").strip().lower()
    if address == (held.email or "").strip().lower():          # the person's own thread
        return PERSON_SUBJECT, thread_headers(held.id, recipient)
    if address == (held.supervisor_email or "").strip().lower():   # the supervisor who held it
        return _supervisor_subject(held), thread_headers(held.id, recipient)
    return None


# ----- admin view -----

async def list_held_requests(include_closed: bool = False) -> list[dict]:
    """Held requests for the admin dashboard, newest first.

    For each row it also reports whether the person is on staff now and which
    supervisor the request would go to when retried, so the admin can see at a
    glance what will happen (#445). The open-status set now also includes
    "not_on_team" (a request waiting for the person to pick another supervisor).

    Args:
        include_closed: Also return released and cancelled rows.

    Returns:
        Plain dicts, snake_case, with a readable "summary", the on-staff flag and
        the supervisor it routes to.
    """
    async with async_session() as session:
        query = select(HeldRequest).order_by(HeldRequest.id.desc())
        if not include_closed:
            query = query.where(HeldRequest.status.in_(ACTIVE_STATUSES))   # includes not_on_team now
        rows = (await session.scalars(query)).all()

    # Whether each address is on staff now; one read per distinct address, so the
    # list shows "on staff" without the admin guessing. A failed read degrades to
    # "unknown" (None) rather than failing the whole list.
    on_staff_by_email: dict[str, bool] = {}
    for address in {r.email for r in rows}:
        try:
            on_staff_by_email[address] = bool(await get_employee_by_email(address))
        except Exception:  # noqa: BLE001 - one bad lookup must not break the list
            logger.exception("Could not check whether %s is on staff for the held list", address)
            on_staff_by_email[address] = False
    return [
        {
            "id": r.id, "email": r.email, "name": r.name,
            "department": r.department or "", "location": r.location or "",
            "supervisor_name": r.supervisor_name, "request_type": r.request_type,
            "summary": describe_form(r.request_type, r.form_data), "status": r.status,
            # Whether they are on staff now, and the supervisor the retry routes to.
            "on_staff": on_staff_by_email.get(r.email, False),
            "routes_to": r.supervisor_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "supervisor_reminded_at": r.supervisor_reminded_at.isoformat() if r.supervisor_reminded_at else None,
            "admins_notified_at": r.admins_notified_at.isoformat() if r.admins_notified_at else None,
            "sp_item_id": r.sp_item_id, "last_error": r.last_error,
            "cancel_reason": r.cancel_reason,
        }
        for r in rows
    ]


async def cancel_held_request(held_id: int, reason: str) -> bool:
    """Cancel one open held request, with a reason, so it can be undone (#445).

    The status it had before is remembered so the cancel can be reversed, and the
    reason is stored. A supervisor still asked to add the person is told it was
    cancelled, in the same conversation.

    Args:
        held_id: The held_requests row id.
        reason: Why it is being cancelled; required and stored.

    Returns:
        True when an open row was cancelled.

    Raises:
        HoldError: When no reason is given.
    """
    reason = (reason or "").strip()
    if not reason:                                           # a reason is mandatory now (#445)
        raise HoldError("A reason is required to cancel a held request.")
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        if row is None or row.status not in CANCELLABLE_STATUSES:  # closed, mid-submit, or no such row
            return False
        row.status_before_cancel = row.status               # remember it, so the undo can restore it
        row.status = "cancelled"
        row.cancel_reason = reason
        await session.commit()
        await session.refresh(row)
    # Only a supervisor still asked to add the person has anything open; close
    # their conversation (#462). Earlier supervisors were told when it moved off
    # them, and one who already added the person or declined has nothing waiting.
    if row.status_before_cancel == "held":
        try:
            await send_email(
                to=[row.supervisor_email], subject=_supervisor_subject(row),
                html_body=(f"<p>{html.escape(row.name)}'s request was cancelled. "
                           "Nothing is needed from you.</p>"),
                headers=thread_headers(row.id, row.supervisor_email),   # the same conversation
            )
        except Exception:  # noqa: BLE001 - informational only
            logger.exception("Could not tell the supervisor about cancelled request #%s", held_id)
    return True


async def uncancel_held_request(held_id: int) -> bool:
    """Undo a cancel, restoring the status the request had before (#445).

    Args:
        held_id: The held_requests row id.

    Returns:
        True when a cancelled row was restored; False when it was not cancelled
        or there is no such row.

    Raises:
        HoldError: When the person has made another request since, so restoring
            this one would give them two at once (#442).
    """
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        if row is None or row.status != "cancelled":         # nothing to undo
            return False
        other = await session.scalar(
            select(HeldRequest.id).where(
                HeldRequest.email == row.email, HeldRequest.id != row.id,
                HeldRequest.status.in_(ACTIVE_STATUSES),
            ).limit(1)
        )
        if other is not None:                                # one request until set up
            raise HoldError(f"They have made another request since (#{other}). Cancel that one first.")
        # Restore the status it held before (defaulting to "held" for rows cancelled
        # before this was tracked; a row cancelled mid-submit by older code comes
        # back as "failed" so an admin can retry it), and clear the cancel reason.
        before = row.status_before_cancel or "held"
        row.status = "failed" if before == "releasing" else before
        row.status_before_cancel = None
        row.cancel_reason = None
        await session.commit()
    return True


# ----- the new hire's waiting page -----

def held_status_token(held_id: int) -> str:
    """A signed token that lets the page that held a request ask how it is going.

    The page that held the request keeps this, so it can show "sent to your
    supervisor" the moment they add the person, without a sign-in.

    Args:
        held_id: The held_requests row id.

    Returns:
        "<id>.<signature>": the id plus an HMAC over it, so no other id can be asked about.
    """
    message = f"held-status:{held_id}".encode()                # its own prefix: never valid as another token
    signature = hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    return f"{held_id}.{signature}"


def held_id_from_status_token(token: str) -> int | None:
    """The held request id a status token names, when its signature checks out.

    Args:
        token: A token from held_status_token.

    Returns:
        The id, or None for a malformed or forged token.
    """
    head, _, _ = (token or "").partition(".")                  # "<id>.<signature>"
    if not head.isdigit():
        return None                                            # not a token at all
    held_id = int(head)
    # Constant-time compare, so the signature cannot be guessed one byte at a time.
    return held_id if hmac.compare_digest(token, held_status_token(held_id)) else None


async def held_request_status(held_id: int) -> dict | None:
    """Where one held request stands, in the words the waiting page needs.

    Args:
        held_id: The held_requests row id.

    Returns:
        {"status": "waiting" | "sent" | "cancelled" | "not_on_team",
        "supervisor_name"}, or None when there is no such row. Every ordinary open
        status (held, waiting_site, releasing, failed) reads as waiting: to the
        person it is still on its way. A "not_on_team" row (a supervisor said the
        person is not on their team) carries the extra fields the page needs to
        let them pick another supervisor: department, summary and who declined it
        (#443).
    """
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
    if row is None:
        return None
    status = {
        "released": "sent", "cancelled": "cancelled", "not_on_team": "not_on_team",
    }.get(row.status, "waiting")
    result = {"status": status, "supervisor_name": row.supervisor_name}
    if status == "not_on_team":
        # The page may re-pick, so hand it the department to search in, the
        # request summary to show, and who declined it. The ordinary waiting/sent
        # payloads are left exactly as they were.
        result.update({
            "department": row.department or "",
            "summary": describe_form(row.request_type, row.form_data),
            "declined_by": row.supervisor_name,
        })
    if row.status == "released" and row.sp_item_id:
        # Released: there is now a real request to follow, so hand the page its
        # status token and it keeps the #364 progress steps to approved/rejected
        # after sign-in (#482). Only on the Postgres path, the same condition the
        # submit response uses; on SharePoint there is no row to follow.
        token = await _released_request_status_token(row)
        if token:
            result["request_status_token"] = token
    return result


async def _released_request_status_token(row: HeldRequest) -> str | None:
    """The progress status token for a released held row's real request (#482).

    Args:
        row: A released held request (``sp_item_id`` is the created request).

    Returns:
        The token to follow the request with, or None off the Postgres path
        (where there is no row to follow) or when the kind/id cannot be named.
    """
    from app.repositories.request_store import requests_in_postgres   # local: avoid an import cycle
    from app.services import request_status
    if not await requests_in_postgres():
        return None                                          # SharePoint path: nothing to follow
    return request_status.status_token_for(row.request_type, row.sp_item_id)


async def released_sign_in_target(held_id: int) -> dict | None:
    """Who a just-released held row may sign in as, and the request to follow (#482).

    The held status token was only ever given to the device that proved the
    address, and the supervisor confirmed who the person is, so a release is
    treated as a sign-in for a short window (``RELEASE_SIGN_IN_DAYS``). This says
    whether that window is open and, if so, hands back the person's email (to
    find their staff record) and the released request's status token.

    Args:
        held_id: The held request the status token named.

    Returns:
        None when there is no such row, it is not released, or its release is
        older than ``RELEASE_SIGN_IN_DAYS`` (the caller then answers 409).
        Otherwise ``{"email", "request_status_token"}``; the token is None off
        the Postgres path.
    """
    row = await _load_held(held_id)
    if row is None or row.status != "released" or row.released_at is None:
        return None                                          # nothing to sign in from
    if _as_utc(utcnow()) - _as_utc(row.released_at) > timedelta(days=RELEASE_SIGN_IN_DAYS):
        return None                                          # released too long ago: sign in with email instead
    return {"email": row.email, "request_status_token": await _released_request_status_token(row)}


async def held_request_email(held_id: int) -> str | None:
    """The email on one held request, for an admin retry."""
    async with async_session() as session:
        row = await session.get(HeldRequest, held_id)
        return row.email if row else None


# ----- the manager dashboard's held new hires (#481) -----

async def held_waiting_on_supervisor(supervisor_id: str) -> list[dict]:
    """Held new hires still waiting on one supervisor, for their dashboard panel (#481).

    Only rows this supervisor currently holds and still has to act on: status
    "held" with ``supervisor_id`` matching. A "not_on_team" row (the other
    ``SUPERVISOR_STATUSES`` value) is excluded on purpose, because it has already
    left this supervisor and waits on the person to re-pick, so the "Not on my
    team" action would be a no-op; "held" is exactly the state
    ``not_on_team_link_state`` and ``held_for_add`` treat as "open"/still theirs.

    Args:
        supervisor_id: The signed-in manager's Staff Directory id.

    Returns:
        One dict per waiting row, oldest first, each with ``id``, ``name``,
        ``email``, ``department``, ``request_type``, ``summary`` (describe_form),
        ``created_at`` (ISO) and ``status``.
    """
    holder = str(supervisor_id or "").strip()                 # the manager's own staff id, as stored
    async with async_session() as session:
        rows = (await session.scalars(
            select(HeldRequest)
            .where(HeldRequest.supervisor_id == holder, HeldRequest.status == "held")
            .order_by(HeldRequest.id)                         # oldest first, like the admin list reversed
        )).all()
    return [
        {
            "id": r.id, "name": r.name, "email": r.email,
            "department": r.department or "",
            "request_type": r.request_type,
            "summary": describe_form(r.request_type, r.form_data),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "status": r.status,
        }
        for r in rows
    ]
