"""The public request page's sign-in: email a code, then check it.

Two unauthenticated endpoints, mounted at /api/intake:

* POST /code   - email a 6-digit code to the address given. Same answer for
                 every address, so the page never reveals who is on staff.
* POST /verify - check the code. A Staff Directory employee gets a 30-day
                 employee dashboard token (the same signed token the emailed
                 links carry). Anyone else gets a short "verified email" token
                 to carry into the new-hire path.
* GET /departments, GET /supervisor-search, POST /held - the new-hire
                 path, all needing that verified-email token: the departments
                 to pick from, a typed search for the supervisor in one, then
                 hold a request until the supervisor adds the person
                 (services/held_requests.py).
* GET /held/status - how one held request is going, for the page that held
                 it; needs the signed status token POST /held returned.
* POST /session/refresh - re-check a signed-in person's role on each visit, so
                 someone made a manager after signing in gets My team (#376).

Mailbox control is the identity proof. Sending goes through send_email, so
whichever email service that routes to (SMTP2GO today; the UCSH mailer for
UCSH addresses once it is switched on) needs no change here.
"""

import html
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import settings

from app.graph.email import send_email
from app.services import email_codes, held_requests, request_status
from app.services.app_settings import carryover_payout_window, toronto_today, window_is_open
from app.services.dashboard_tokens import generate_dashboard_token, validate_dashboard_token
from app.services.employee import (
    email_belongs_to_archived,
    get_employee_by_email,
    get_employee_by_id,
    get_employees_by_email,
    is_manager,
)
from app.services.manager_match import manager_name_of  # who manages whom, by id or name (#376)
from app.repositories.postgres.staff import is_archived  # tells an archived record apart (#119)
from app.services.request_intake import RequestFormError

logger = logging.getLogger(__name__)
router = APIRouter()

# Identical for every address and for a rate-limited request, so neither who is
# on staff nor how often someone asked can be read off the answer.
_CODE_SENT = {
    "status": "sent",
    "detail": "If that address can receive email, a 6-digit code is on its way. It works for 10 minutes.",
}


class CodeRequest(BaseModel):
    """Body of POST /code."""
    email: str


class VerifyRequest(BaseModel):
    """Body of POST /verify."""
    email: str
    code: str


class SessionBody(BaseModel):
    """Body of POST /session/refresh: the signed-in token the page holds."""
    token: str
    role: str
    uid: str
    exp: str


class VerifiedEmail(BaseModel):
    """The token /verify hands to someone not on staff."""
    email: str
    exp: str
    token: str


class HeldRequestBody(BaseModel):
    """Body of POST /held: who they are, who supervises them, and the form.

    The supervisor is supervisor_id (picked from the search) or
    supervisor_email (typed when the search did not find them), not both.
    """
    verified: VerifiedEmail
    first_name: str
    last_name: str
    department: str
    supervisor_id: str | None = None
    supervisor_email: str | None = None
    request_type: str
    form: dict


def _require_verified(email: str, exp: str, token: str) -> str:
    """Check a verified-email token and return its normalised address.

    Args:
        email: The address the token claims.
        exp: Its expiry.
        token: The signature.

    Returns:
        The normalised address.

    Raises:
        HTTPException: 401 when the token is wrong or expired.
    """
    if not email_codes.check_verified_email(email, exp, token):
        raise HTTPException(status_code=401, detail="Your email check has expired. Start again.")
    return email_codes.normalise_email(email)


def _client_ip(request: Request) -> str:
    """The caller's IP for rate limiting, preferring the proxy's header.

    Railway sits behind a proxy, so the socket address is the proxy. The
    caller can put anything at the front of X-Forwarded-For, so its first
    entry is never trusted: the proxy's own X-Real-IP comes first, then the
    last X-Forwarded-For entry (the one the proxy appended itself).

    Args:
        request: The incoming request.

    Returns:
        A best-effort IP string, "unknown" when nothing is available.
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip                                         # set by the proxy, not the caller
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()                # last hop was added by the proxy
    return request.client.host if request.client else "unknown"


@router.post("/code")
async def request_code(body: CodeRequest, request: Request):
    """Email a sign-in code to the address, within the send limits.

    Args:
        body: The address the person typed.
        request: Used for the caller's IP.

    Returns:
        The same acknowledgement whatever happened, except when the email
        service itself failed.

    Raises:
        HTTPException: 400 for an empty or obviously invalid address; 502 when
            the email could not be handed to the email service.
    """
    email = email_codes.normalise_email(body.email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Enter a full email address.")

    result = await email_codes.issue_code(email, _client_ip(request))
    if result.code is None:
        logger.info("Email code not sent: %s", result.reason)  # address not logged
        return _CODE_SENT

    try:
        await send_email(
            to=[email],
            subject="Your UCSH Out of Office code",
            html_body=_render_code_email(result.code),
        )
    except Exception:  # noqa: BLE001 - a failed send is reported, never a crash
        logger.exception("Could not send an email code")
        raise HTTPException(
            status_code=502,
            detail="The code could not be sent right now. Try again in a few minutes.",
        )
    return _CODE_SENT


async def _manages_anyone(employee: dict) -> bool | None:
    """Whether anyone in the Staff Directory lists this person as a manager.

    Matches by their link id or by name ignoring case and spacing (#376), so a
    manager set up outside the app is found too.

    Args:
        employee: The person's staff record (``{"id", "fields"}``).

    Returns:
        True for a manager, False when nobody lists them, None when the check
        failed (sign-in treats that as an employee; the next visit's refresh
        tries again instead of keeping the wrong answer for 30 days).
    """
    if not (employee.get("fields", {}) or {}).get("Title"):
        return False                                           # a blank record manages nobody
    try:
        return await is_manager(await manager_name_of(employee))
    except Exception:  # noqa: BLE001 - a failed lookup must not block sign-in
        logger.exception("Could not check whether the signed-in person is a manager")
        return None


@router.post("/verify")
async def verify_code(body: VerifyRequest):
    """Check a code, then say who the person is.

    Args:
        body: The address and the code typed.

    Returns:
        For a Staff Directory employee: {"status": "employee", "name", and the
        dashboard token fields role/uid/token/exp}. role is "manager" when
        someone lists them as a manager (it opens My team as well as their own
        pages), otherwise "employee". When more than one Staff Directory record
        carries the address (a shared mailbox): {"status": "choose", "people":
        [one employee answer per record, plus "department" and "location"]}, so
        the page asks which person is signing in. For anyone else:
        {"status": "unknown", "verified": {email, exp, token}}.

    Raises:
        HTTPException: 400 with a readable reason when the code is wrong,
            expired, used up or missing.
    """
    email = email_codes.normalise_email(body.email)
    result = await email_codes.check_code(email, body.code)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error)

    people = await get_employees_by_email(email)            # active records only (archived filtered out)
    if len(people) > 1:
        # A shared mailbox: whoever controls it may sign in as any record on it,
        # so each gets its own token and the page asks which one this is.
        return {
            "status": "choose",
            "people": [await _signed_in_as(employee) for employee in people],
        }
    if people:
        answer = {"status": "employee", **await _signed_in_as(people[0])}
        # One request until set up (#442): while this person has a held request in
        # flight, hand the page its status and a verified-email token, so it shows
        # the status instead of a new form and can re-pick a supervisor.
        held = await _held_gate(email=email, statuses=held_requests.SUPERVISOR_STATUSES)   # on staff: only these block
        if held:
            answer["held"] = held
            answer["verified"] = email_codes.sign_verified_email(email)
        return answer

    # No active record. If an archived one carries this address, the person has
    # left: refuse with a plain message rather than send them down the new-hire
    # path (#119). An archived person cannot make a request.
    if await email_belongs_to_archived(email):
        raise HTTPException(
            status_code=403,
            detail="This account has been archived. Contact HR if you think this is a mistake.",
        )

    # Not on staff yet: prove the address was verified for the new-hire path.
    answer = {"status": "unknown", "verified": email_codes.sign_verified_email(email)}
    # One request until set up (#442): a new hire who already has a request
    # waiting sees its status instead of the new-hire form.
    held = await _held_gate(email=email)
    if held:
        answer["held"] = held
    return answer


async def _held_gate(*, email: str, statuses: tuple = held_requests.ACTIVE_STATUSES) -> dict | None:
    """The status of a person's in-flight held request, for the sign-in gate (#442).

    Args:
        email: Match a held request on the person's address.
        statuses: Which held statuses count (fewer for someone already on staff).

    Returns:
        ``{"status_token", "supervisor_name", "state"}`` when they have a request
        waiting to be set up, or None when they do not. ``state`` is "not_on_team"
        when a supervisor handed it back (so the page offers a re-pick), else
        "waiting".
    """
    row = await held_requests.open_held_request_for(email=email, statuses=statuses)
    if row is None:
        return None
    return {
        "status_token": held_requests.held_status_token(row.id),
        "supervisor_name": row.supervisor_name,
        "state": "not_on_team" if row.status == "not_on_team" else "waiting",
    }


async def _signed_in_as(employee: dict) -> dict:
    """The sign-in answer for one staff record: who they are, plus their token.

    Args:
        employee: The staff record (``{"id", "fields"}``).

    Returns:
        {"name", "department", "location", role, uid, token, exp}. role is
        "manager" when someone lists them as a manager, otherwise "employee".
    """
    fields = employee.get("fields", {})
    name = fields.get("Title", "")
    role = "manager" if await _manages_anyone(employee) else "employee"   # None (failed) -> employee
    # Same signed token the emailed dashboard links carry; 30 days by default.
    session_token = generate_dashboard_token(role, employee["id"])
    return {
        "name": name,
        "department": fields.get("Department") or "",          # tells two records apart on the chooser
        "location": fields.get("Location") or "",
        **session_token,                                       # role, uid, token, exp
    }


@router.get("/departments")
async def departments(email: str = Query(...), exp: str = Query(...), token: str = Query(...)):
    """The departments a person not on staff can pick from.

    Needs the verified-email token, so the staff list is never public.

    Args:
        email: Verified-email token fields, as query parameters.
        exp: See email.
        token: See email.

    Returns:
        {"departments": [...]} as the Staff Directory spells them.
    """
    _require_verified(email, exp, token)
    return {"departments": await held_requests.list_departments()}


@router.get("/supervisor-search")
async def supervisor_search(
    department: str = Query(...), q: str = Query(""),
    email: str = Query(...), exp: str = Query(...), token: str = Query(...),
):
    """People in a department whose name contains the typed text.

    Anyone in the department, so a newly made supervisor is found too. Only a
    few names per search, and none until two letters are typed.

    Args:
        department: The department picked.
        q: What was typed.
        email: Verified-email token fields, as query parameters.
        exp: See email.
        token: See email.

    Returns:
        {"people": [{"id", "name"}]}.
    """
    _require_verified(email, exp, token)
    return {"people": await held_requests.search_department(department, q)}


@router.get("/request-types")
async def request_types():
    """Which request types the page may offer right now (#176).

    Public and read-only: the page asks before showing the type cards, so a
    closed type is greyed out. The submit endpoints check again, so this
    answer is only a convenience.

    Returns:
        {"carryover_payout_open": bool, "carryover_payout_window":
        {"start", "end"} or None, "today": the Toronto date checked}.
    """
    window, today = await carryover_payout_window(), toronto_today()
    return {
        "carryover_payout_open": window_is_open(window, today),
        "carryover_payout_window": (
            {"start": window[0].isoformat(), "end": window[1].isoformat()} if window else None
        ),
        "today": today.isoformat(),                            # so the page words "opens on" the same way
    }


@router.post("/held")
async def hold(body: HeldRequestBody):
    """Hold a request from someone not on staff and email their supervisor.

    Args:
        body: The verified-email token, name, department, supervisor and form.

    Returns:
        {"status": "held", "supervisor_name", "status_token"}: the token lets
        the page ask GET /held/status how the request is going.

    Raises:
        HTTPException: 401 for a bad token; 409 when the address is on staff
            after all (sign in again); 400 for anything wrong with the form;
            503 while processing is off.
    """
    email = _require_verified(body.verified.email, body.verified.exp, body.verified.token)
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Requests cannot be submitted right now. Try again later.")
    if await get_employee_by_email(email):
        # Added since they verified: the normal path works now.
        raise HTTPException(status_code=409, detail="You are in the Staff Directory now. Start again to sign in.")
    try:
        row = await held_requests.hold_request(
            email, f"{body.first_name} {body.last_name}", body.department,
            body.supervisor_id, body.request_type, body.form,
            supervisor_email=body.supervisor_email,
        )
    except (held_requests.HoldError, RequestFormError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "status": "held", "supervisor_name": row.supervisor_name,
        "status_token": held_requests.held_status_token(row.id),   # the waiting page polls with it
    }


class RepickBody(BaseModel):
    """Body of POST /held/repick: pick another supervisor after a "not on my team".

    Bound to the verified-email token (so only the person who proved the address
    may re-route it) and the held request's status token (which names the row).
    """
    verified: VerifiedEmail
    status_token: str
    supervisor_id: str | None = None
    supervisor_email: str | None = None


@router.post("/held/repick")
async def held_repick(body: RepickBody):
    """Pick another supervisor for a held request handed back as "not on my team" (#443).

    Only the person who owns the request (their verified email) may re-pick, only
    while it is waiting for a new supervisor, and never the one who declined it.
    The request's dates and form do not change.

    Args:
        body: The verified-email token, the status token, and the new supervisor.

    Returns:
        ``{"status": "held", "supervisor_name", "status_token"}``.

    Raises:
        HTTPException: 401 for a bad verified-email token; 404 for a bad status
            token; 400 when it is not theirs, not waiting for a re-pick, or the
            new supervisor cannot be used.
    """
    email = _require_verified(body.verified.email, body.verified.exp, body.verified.token)
    if not settings.PROCESSING_ENABLED:                         # read-only: nothing moves, nobody is emailed
        raise HTTPException(status_code=503, detail="Requests cannot be changed right now. Try again later.")
    held_id = held_requests.held_id_from_status_token(body.status_token)
    if held_id is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    try:
        row = await held_requests.repick_supervisor(
            held_id, email, body.supervisor_id, body.supervisor_email,
        )
    except held_requests.HoldError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "status": "held", "supervisor_name": row.supervisor_name,
        "status_token": held_requests.held_status_token(row.id),
    }


def _page(title: str, body_html: str) -> str:
    """One small standalone HTML page for a "Not on my team" link.

    Args:
        title: The page title and heading.
        body_html: The body markup under the heading (already safe).

    Returns:
        A full HTML document string.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.4rem'>{title}</h1>"
        f"{body_html}"
        "</body></html>"
    )


def _state_page(state: dict) -> str:
    """The page a valid "Not on my team" link shows once its request has moved on (#462).

    Args:
        state: From ``held_requests.not_on_team_link_state``; not "open".

    Returns:
        A full HTML document saying what really happened; every value is escaped.
    """
    name = html.escape(state.get("name") or "This person")
    if state["state"] == "released":
        number = html.escape(state.get("request_number") or "")
        on = f" on {html.escape(state['set_up_on'])}" if state.get("set_up_on") else ""
        return _page("Already handled", f"<p>{name} was set up{on}. Their request is #{number}.</p>")
    if state["state"] == "cancelled":
        return _page("No longer needed", f"<p>{name}'s request was cancelled. Nothing is needed from you.</p>")
    if state["state"] == "sending":
        return _page("Already handled", f"<p>{name} is on staff now and their request is being sent.</p>")
    # "declined": they said not on their team earlier, or it moved off them.
    return _page("Already done", f"<p>You already let us know {name} is not on your team. "
                                 "Nothing is needed from you.</p>")


@router.get("/held/not-on-team", response_class=HTMLResponse)
async def held_not_on_team_page(token: str = Query(...)):
    """Show the "Not on my team" confirmation page from a supervisor's email (#443).

    Public and signed, and read-only: this GET changes nothing. Mail link scanners
    (Microsoft 365 Safe Links) fetch every link before a person clicks, so acting
    on the GET would hand back every request automatically. Instead it shows a page
    with one button that POSTs the same token to do the decline. A forged link gets
    that same page and learns nothing; a valid link whose request has moved on
    (declined, set up, cancelled, being sent) says what happened instead (#462).

    Args:
        token: The signed not-on-team token from the email.

    Returns:
        A small HTML page with a confirm button that re-POSTs this same URL.
    """
    # A valid link whose request has moved on says what happened instead (#462).
    decoded = held_requests.decode_not_on_team_token(token)
    if decoded is not None:
        state = await held_requests.not_on_team_link_state(*decoded)
        if state is not None and state["state"] != "open":
            return HTMLResponse(_state_page(state))
    # The form has no action, so it re-posts this exact URL (token and all); the
    # token is never written into the HTML, so it cannot be injected.
    return HTMLResponse(_page(
        "Not on your team?",
        "<p>Let us know this person is not on your team and we will ask them to pick "
        "their supervisor again. You do not need to do anything else.</p>"
        "<form method='post'>"
        "<button type='submit' style='font:inherit;padding:.6rem 1rem;border:0;"
        "border-radius:.4rem;background:#1565c0;color:#fff;cursor:pointer'>"
        "They are not on my team</button>"
        "</form>",
    ))


@router.post("/held/not-on-team", response_class=HTMLResponse)
async def held_not_on_team_submit(token: str = Query(...)):
    """Hand a held request back to be re-picked, from the confirm page's button (#443).

    Public and signed: the token names one held request and the supervisor it was
    issued to, so a stale link cannot act once the request has moved on. Hands the
    request back so the person can pick another supervisor (guarded and
    idempotent), and shows a short page. A forged link gets the ordinary thanks; a
    valid link whose request had already moved on says what happened (#462).

    Args:
        token: The signed not-on-team token, submitted by the confirm page.

    Returns:
        A small HTML page confirming it was handed back.
    """
    if not settings.PROCESSING_ENABLED:                         # read-only: hand nothing back, email nobody
        return HTMLResponse(_page(
            "Not right now",
            "<p>This cannot be done right now. Open the link again later.</p>",
        ), status_code=503)
    decoded = held_requests.decode_not_on_team_token(token)
    if decoded is not None:
        held_id, supervisor_id = decoded
        outcome = await held_requests.decline_held_request(held_id, supervisor_id)   # guarded + idempotent
        if outcome == "already":                                # it had moved on: say what happened (#462)
            state = await held_requests.not_on_team_link_state(held_id, supervisor_id)
            if state is not None:
                return HTMLResponse(_state_page(state))
    return HTMLResponse(_page(
        "Thanks for letting us know",
        "<p>We have asked them to pick their supervisor again. You do not need to do anything else.</p>",
    ))


@router.get("/held/status")
async def held_status(token: str = Query(...)):
    """How a held request is going, for the page that held it.

    Public, but only for the one request its signed token names: the answer is
    the status and the supervisor the person already chose, nothing more. The
    page asks every few seconds, so it says "sent" the moment the supervisor
    adds the person.

    Args:
        token: The status_token POST /held returned.

    Returns:
        {"status": "waiting" | "sent" | "cancelled", "supervisor_name"}.

    Raises:
        HTTPException: 404 for a bad token or a request that no longer exists.
    """
    held_id = held_requests.held_id_from_status_token(token)
    found = await held_requests.held_request_status(held_id) if held_id is not None else None
    if found is None:
        raise HTTPException(status_code=404, detail="Request not found.")   # same answer for forged and gone
    return found


@router.get("/request-status")
async def request_status_endpoint(token: str = Query(...)):
    """How a request sent from the request page is going, for the page that sent it.

    Public, but only for the one request its signed token names (#364). The
    normal submit response (routes/self_service.py) hands the page this token;
    the page asks every few seconds and shows each step done with its time, so an
    approval by email, text or dashboard shows without a sign-in or a refresh.

    Args:
        token: The status_token the submit response returned.

    Returns:
        {"status", "manager_name", "finished", "steps": {...}} for that request
        (see services/request_status.py).

    Raises:
        HTTPException: 401 for a forged or malformed token; 404 when the request
            it names does not exist (e.g. on the SharePoint path).
    """
    reference = request_status.request_ref_from_status_token(token)
    if reference is None:
        raise HTTPException(status_code=401, detail="This status link is not valid.")
    kind, item_id = reference
    found = await request_status.request_progress(kind, item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    return found


def _render_code_email(code: str) -> str:
    """HTML body of the code email.

    Args:
        code: The 6-digit code.

    Returns:
        A short HTML body for send_email.
    """
    return (
        "<p>Your UCSH Out of Office code is:</p>"
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:6px">{code}</p>'
        "<p>It works for 10 minutes. If you did not ask for it, you can ignore this email.</p>"
    )


@router.post("/session/refresh")
async def refresh_session(body: SessionBody):
    """Re-check a signed-in person's role, so My team follows the Staff Directory (#376).

    The role in a sign-in token is decided when the code is checked and kept for
    30 days. Someone made a manager after that (or whose check failed that day)
    would not see My team until the token ran out. The page calls this on every
    visit; when the person has since become a manager it gets a manager token.

    This only ever promotes an employee to manager; it never demotes a manager
    back to employee (#395). A supervisor opening the add-this-new-hire link
    before anyone is under them, and an admin's open-as-manager link, keep their
    manager role (and Add Employee) rather than being dropped to employee just
    because nobody lists them yet.

    Args:
        body: The token fields the page holds (from the code sign-in or an
            emailed link).

    Returns:
        ``{"changed": False}`` when there is nothing to promote (already a
        manager or admin, still only an employee, or the check could not run),
        else ``{"changed": True, role, uid, token, exp}`` with a manager token.

    Raises:
        HTTPException: 401 when the token is not valid; 404 when the person's
            record is gone; 403 when they have been archived.
    """
    valid, error = validate_dashboard_token(body.role, body.uid, body.token, body.exp)
    if not valid:
        raise HTTPException(status_code=401, detail=error)
    if body.role not in ("employee", "manager"):              # an admin link keeps its role
        return {"changed": False}
    employee = await get_employee_by_id(body.uid)             # their record now
    if not employee:
        raise HTTPException(status_code=404, detail="Your staff record could not be found.")
    if is_archived(employee.get("fields", {})):               # a departed person gets no new token (#119)
        raise HTTPException(status_code=403, detail="This account is no longer active. Please contact HR.")
    # Only ever promote (#395): a manager link is never demoted to employee, even
    # when nobody lists them yet, so it keeps My team and Add Employee. Only an
    # employee is re-checked, to raise them up once someone lists them.
    if body.role == "manager":                                # already a manager: nothing to raise
        return {"changed": False}
    manages = await _manages_anyone(employee)
    if not manages:                                           # None (check failed) or False: stay an employee
        return {"changed": False}
    # Someone now lists this employee as a manager: hand back a manager token for
    # the page to swap in for the old one.
    return {"changed": True, **generate_dashboard_token("manager", body.uid)}
