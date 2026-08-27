"""Self-service sign-in: email a signed dashboard link to a known @ucsh address.

Replaces "dig through an old email to find your dashboard link". A person enters
their work email; if it matches a Staff Directory record, a signed dashboard link
(the same HMAC link the app already mints) is emailed to that mailbox. Mailbox
control is the identity proof, which holds across both Microsoft tenants without
any Entra unification.

Admin is deliberately excluded — management distributes the admin link itself
(the tokenless bookmark model), so this only ever mints employee/manager links.
"""
import logging
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.graph.email import send_email
from app.services.dashboard_tokens import generate_dashboard_url
from app.services.employee import get_employee_by_email, get_employee_roles

logger = logging.getLogger(__name__)
router = APIRouter()

# Roles a self-service link may carry, manager first. Admin is intentionally
# omitted (management hands out the tokenless admin link itself).
_SELF_SERVICE_ROLES = ("manager", "employee")

# Identical wording whether or not the email matched, so the endpoint cannot be
# used to discover which addresses are on file.
_GENERIC_RESPONSE = {
    "status": "ok",
    "detail": "If that email is on file, a sign-in link has been sent to it.",
}

# --- Rate limiting (in-memory, per instance) --------------------------------
_MAX_PER_WINDOW = 3           # links allowed per key per window
_WINDOW_SECONDS = 900         # 15-minute sliding window
_hits: dict[str, list[float]] = {}   # key -> recent request timestamps


def _rate_limited(key: str) -> bool:
    """Report whether ``key`` is over its window, recording this hit if not.

    In-memory and per-instance: enough to blunt email-bombing and enumeration on
    a single-instance deployment. A shared store would be needed to enforce it
    across multiple instances.

    Args:
        key: The bucket to count against (an email or a client IP).

    Returns:
        True if this request exceeds the window (and is not recorded); False
        otherwise (and the hit is recorded).
    """
    now = time.time()                                          # window is wall-clock based
    recent = [t for t in _hits.get(key, []) if now - t < _WINDOW_SECONDS]  # drop expired hits
    if len(recent) >= _MAX_PER_WINDOW:                         # already at the cap
        _hits[key] = recent                                   # keep window, refuse this hit
        return True
    recent.append(now)                                        # record the allowed hit
    _hits[key] = recent
    return False


class LinkRequest(BaseModel):
    """Body of a sign-in-link request."""
    email: str


@router.post("/request-link")
async def request_link(body: LinkRequest, request: Request):
    """Email a signed dashboard link to the address if it maps to an employee.

    Args:
        body: The requested email address.
        request: Used only for the client IP, for rate limiting.

    Returns:
        The same generic acknowledgement regardless of whether the email
        matched — it never reveals whether an address is on file.
    """
    email = (body.email or "").strip().lower()                # normalise for lookup + rate key
    client_ip = request.client.host if request.client else "unknown"  # best-effort source

    # Rate-limit on both the email and the caller IP, so neither an address nor a
    # source can be hammered; an over-limit request still returns the generic body.
    if _rate_limited(f"email:{email}") or _rate_limited(f"ip:{client_ip}"):
        logger.info("Sign-in link rate-limited (ip=%s)", client_ip)  # address not logged
        return _GENERIC_RESPONSE

    employee = await get_employee_by_email(email)             # through the repository seam
    if employee is None:                                      # unknown address
        logger.info("Sign-in link requested for an unrecognised email")  # address not logged
        return _GENERIC_RESPONSE

    roles = await get_employee_roles(employee)                # ["employee", maybe "manager"/"admin"]
    link_roles = [r for r in _SELF_SERVICE_ROLES if r in roles]  # admin dropped, manager first
    employee_id = employee["id"]                              # SP item id the URL carries as uid
    links = [
        {"role": r, "url": generate_dashboard_url(r, employee_id)}  # 30-day expiry (default)
        for r in link_roles
    ]

    await send_email(                                         # SMTP2GO; failure is logged, not raised
        to=[email],
        subject="Your UCSH Gone Fishing sign-in link",
        html_body=_render_email(links),
    )
    logger.info("Sign-in link sent to employee #%s (%d link(s))", employee_id, len(links))
    return _GENERIC_RESPONSE


def _render_email(links: list[dict]) -> str:
    """Render the sign-in email body, one link per role the person holds.

    Args:
        links: ``{"role", "url"}`` dicts to offer (at least the employee link).

    Returns:
        An HTML body string for ``send_email``.
    """
    rows = "".join(                                           # one line per available dashboard
        f'<p><a href="{link["url"]}">Open your {link["role"]} dashboard</a></p>'
        for link in links
    )
    return (
        "<p>Here is your UCSH Gone Fishing sign-in link. "
        "It is personal to you — please don't forward it.</p>"
        f"{rows}"
        "<p>The link is valid for 30 days.</p>"
    )
