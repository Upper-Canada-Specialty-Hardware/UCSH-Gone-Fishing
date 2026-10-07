"""The public request page's sign-in: email a code, then check it.

Two unauthenticated endpoints, mounted at /api/intake:

* POST /code   - email a 6-digit code to the address given. Same answer for
                 every address, so the page never reveals who is on staff.
* POST /verify - check the code. A Staff Directory employee gets a 30-day
                 employee dashboard token (the same signed token the emailed
                 links carry). Anyone else gets a short "verified email" token
                 to carry into the new-hire path.

Mailbox control is the identity proof. Sending goes through send_email, so
whichever email service that routes to (SMTP2GO today; HVE and Clerk later)
needs no change here.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.graph.email import send_email
from app.services import email_codes
from app.services.dashboard_tokens import generate_dashboard_token
from app.services.employee import get_employee_by_email

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


def _client_ip(request: Request) -> str:
    """The caller's IP for rate limiting, preferring the proxy's header.

    Railway sits behind a proxy, so the socket address is the proxy; the first
    X-Forwarded-For entry is the real caller.

    Args:
        request: The incoming request.

    Returns:
        A best-effort IP string, "unknown" when nothing is available.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()                 # first hop is the client
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


@router.post("/verify")
async def verify_code(body: VerifyRequest):
    """Check a code, then say who the person is.

    Args:
        body: The address and the code typed.

    Returns:
        For a Staff Directory employee: {"status": "employee", "name", and the
        dashboard token fields role/uid/token/exp}. For anyone else:
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
        # Same signed token the emailed dashboard links carry; 30 days by default.
        session_token = generate_dashboard_token("employee", employee["id"])
        return {
            "status": "employee",
            "name": employee.get("fields", {}).get("Title", ""),
            **session_token,                                   # role, uid, token, exp
        }

    # Not on staff yet: prove the address was verified for the new-hire path.
    return {"status": "unknown", "verified": email_codes.sign_verified_email(email)}


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
