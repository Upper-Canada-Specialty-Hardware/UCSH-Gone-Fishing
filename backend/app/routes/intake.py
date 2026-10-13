"""The public request page's sign-in: email a code, then check it.

Two unauthenticated endpoints, mounted at /api/intake:

* POST /code   - email a 6-digit code to the address given. Same answer for
                 every address, so the page never reveals who is on staff.
* POST /verify - check the code. A Staff Directory employee gets a 30-day
                 employee dashboard token (the same signed token the emailed
                 links carry). Anyone else gets a short "verified email" token
                 to carry into the new-hire path.
* GET /supervisors, POST /held - the new-hire path, both needing that
                 verified-email token: list the supervisors to pick from, then
                 hold a request until the supervisor adds the person
                 (services/held_requests.py).

Mailbox control is the identity proof. Sending goes through send_email, so
whichever email service that routes to (SMTP2GO today; the UCSH mailer for
UCSH addresses once it is switched on) needs no change here.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings

from app.graph.email import send_email
from app.services import email_codes, held_requests
from app.services.dashboard_tokens import generate_dashboard_token
from app.services.employee import LOCATION_PROVINCE_MAP, get_employee_by_email, is_manager
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


class VerifiedEmail(BaseModel):
    """The token /verify hands to someone not on staff."""
    email: str
    exp: str
    token: str


class HeldRequestBody(BaseModel):
    """Body of POST /held: who they are, who supervises them, and the form."""
    verified: VerifiedEmail
    name: str
    location: str
    supervisor_id: str
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


async def _manages_anyone(name: str) -> bool:
    """Whether anyone in the Staff Directory lists this person as a manager.

    Args:
        name: The employee's Staff Directory name.

    Returns:
        True for a manager. False when nobody lists them, or when the check
        fails: they still sign in, just without My team, and the next sign-in
        tries again.
    """
    if not name:
        return False
    try:
        return await is_manager(name)
    except Exception:  # noqa: BLE001 - a failed lookup must not block sign-in
        logger.exception("Could not check whether the signed-in person is a manager")
        return False


@router.post("/verify")
async def verify_code(body: VerifyRequest):
    """Check a code, then say who the person is.

    Args:
        body: The address and the code typed.

    Returns:
        For a Staff Directory employee: {"status": "employee", "name", and the
        dashboard token fields role/uid/token/exp}. role is "manager" when
        someone lists them as a manager (it opens My team as well as their own
        pages), otherwise "employee". For anyone else:
        {"status": "unknown", "verified": {email, exp, token}}.

    Raises:
        HTTPException: 400 with a readable reason when the code is wrong,
            expired, used up or missing.
    """
    email = email_codes.normalise_email(body.email)
    result = await email_codes.check_code(email, body.code)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.error)

    employee = await get_employee_by_email(email)
    if employee is not None:
        name = employee.get("fields", {}).get("Title", "")
        role = "manager" if await _manages_anyone(name) else "employee"
        # Same signed token the emailed dashboard links carry; 30 days by default.
        session_token = generate_dashboard_token(role, employee["id"])
        return {
            "status": "employee",
            "name": name,
            **session_token,                                   # role, uid, token, exp
        }

    # Not on staff yet: prove the address was verified for the new-hire path.
    return {"status": "unknown", "verified": email_codes.sign_verified_email(email)}


@router.get("/supervisors")
async def supervisors(email: str = Query(...), exp: str = Query(...), token: str = Query(...)):
    """The supervisors and locations a person not on staff can pick from.

    Needs the verified-email token, so the staff list is never public.

    Args:
        email: Verified-email token fields, as query parameters.
        exp: See email.
        token: See email.

    Returns:
        {"supervisors": [{"id", "name", "location"}], "locations": [...]}.
    """
    _require_verified(email, exp, token)
    return {
        "supervisors": await held_requests.list_supervisors(),
        "locations": list(LOCATION_PROVINCE_MAP),
    }


@router.post("/held")
async def hold(body: HeldRequestBody):
    """Hold a request from someone not on staff and email their supervisor.

    Args:
        body: The verified-email token, name, location, supervisor and form.

    Returns:
        {"status": "held", "supervisor_name"}.

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
            email, body.name, body.location, body.supervisor_id, body.request_type, body.form,
        )
    except (held_requests.HoldError, RequestFormError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "held", "supervisor_name": row.supervisor_name}


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
