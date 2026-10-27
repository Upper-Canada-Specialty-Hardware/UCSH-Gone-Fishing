"""The public request page's sign-in: email a code, then check it.

Two unauthenticated endpoints, mounted at /api/intake:

* POST /code   - email a 6-digit code to the address given. Same answer for
                 every address, so the page never reveals who is on staff.
* POST /verify - check the code. A Staff Directory employee gets a 30-day
                 employee dashboard token (the same signed token the emailed
                 links carry). An address on no active staff record is refused:
                 setting yourself up is not done here, a manager adds people
                 from the manager dashboard (#526).
* POST /session/refresh - re-check a signed-in person's role on each visit, so
                 someone made a manager after signing in gets My team (#376).

Mailbox control is the identity proof. Sending goes through send_email, so
whichever email service that routes to (SMTP2GO today; the UCSH mailer for
UCSH addresses once it is switched on) needs no change here.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings

from app.graph.email import send_email
from app.services import email_codes, request_status
from app.services.app_settings import carryover_payout_window, toronto_today, window_is_open
from app.services.dashboard_tokens import generate_dashboard_token, validate_dashboard_token
from app.services.employee import (
    email_belongs_to_archived,
    get_employee_by_id,
    get_employees_by_email,
    is_manager,
)
from app.services.manager_match import manager_name_of  # who manages whom, by id or name (#376)
from app.repositories.postgres.staff import is_archived  # tells an archived record apart (#119)

logger = logging.getLogger(__name__)
router = APIRouter()

# Identical for every address and for a rate-limited request, so neither who is
# on staff nor how often someone asked can be read off the answer.
_CODE_SENT = {
    "status": "sent",
    "detail": "If that address can receive email, a 6-digit code is on its way. It works for 10 minutes.",
}


# The refusal for an address on no staff record (#526): what to do next, and
# nothing about who is on staff or who their manager is.
NOT_ON_STAFF = (
    "This email is not on a staff record, so it cannot be used to sign in. "
    "Ask your manager: they can add you, or make the request for you."
)


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
        the page asks which person is signing in.

    Raises:
        HTTPException: 400 with a readable reason when the code is wrong,
            expired, used up or missing; 403 for an archived address or one on
            no staff record (ask your manager).
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
        return {"status": "employee", **await _signed_in_as(people[0])}

    # No active record. If an archived one carries this address, the person has
    # left: refuse with a plain message (#119). An archived person cannot make
    # a request.
    if await email_belongs_to_archived(email):
        raise HTTPException(
            status_code=403,
            detail="This account has been archived. Ask your manager if you think this is a mistake.",
        )

    # On no staff record: refused, never set up from here (#526). The manager
    # adds them, or makes the request for them, from the manager dashboard. No
    # manager is named: the address alone says nothing about who they work for.
    raise HTTPException(status_code=403, detail=NOT_ON_STAFF)


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


@router.get("/cancel/info")
async def cancel_link_info(token: str = Query(...)):
    """What the public cancel page shows for a receipt's cancel link (#519).

    Read-only and public, but only for the one request its signed token names. A
    forged or gone link reads as ``{"state": "invalid"}`` and learns nothing.

    Args:
        token: The signed token from the receipt's link.

    Returns:
        ``{"state", "request_number", "summary"}``; see ``own_requests.cancel_page_info``.
    """
    from app.services.own_requests import cancel_page_info
    return await cancel_page_info(token)


@router.post("/cancel/confirm")
async def cancel_link_confirm(token: str = Query(...)):
    """Cancel the request a receipt's link names, from the page's confirm button (#519).

    A POST, sent only by the button, so a mail scanner opening the link cancels
    nothing. Guarded and idempotent: a second click, or a request decided in the
    meantime, just says what it is now.

    Args:
        token: The signed token from the receipt's link.

    Returns:
        The info shape, with ``state`` "done" when this click cancelled it.

    Raises:
        HTTPException: 503 while processing is off (nothing is cancelled).
    """
    if not settings.PROCESSING_ENABLED:                        # read-only: cancel nothing, email nobody
        raise HTTPException(status_code=503, detail="This cannot be done right now. Try again later.")
    from app.services.own_requests import cancel_from_link
    return await cancel_from_link(token)


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
    back to employee (#395). An admin's open-as-manager link keeps its manager
    role (and Add Employee) rather than being dropped to employee just because
    nobody lists them yet.

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
