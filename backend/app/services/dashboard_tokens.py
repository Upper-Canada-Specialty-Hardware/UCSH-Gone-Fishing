import hashlib
import hmac
import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 30


def generate_dashboard_token(role: str, user_id: str | int, expiry_days: int = DEFAULT_EXPIRY_DAYS) -> dict:
    """Generate a signed dashboard token and URL parameters."""
    expiry = int(time.time()) + expiry_days * 86400
    token = _sign(role, str(user_id), str(expiry))
    return {
        "token": token,
        "role": role,
        "uid": str(user_id),
        "exp": str(expiry),
    }


def generate_dashboard_url(role: str, user_id: str | int, expiry_days: int = DEFAULT_EXPIRY_DAYS) -> str:
    """Generate a full dashboard URL with signed token."""
    params = generate_dashboard_token(role, user_id, expiry_days)
    base = settings.DASHBOARD_FRONTEND_URL.rstrip("/")
    return f"{base}/#/dashboard?token={params['token']}&role={params['role']}&uid={params['uid']}&exp={params['exp']}"


def validate_dashboard_token(role: str, user_id: str, token: str, expiry: str) -> tuple[bool, str]:
    """Validate a dashboard HMAC token. Returns (valid, error_message)."""
    try:
        exp_int = int(expiry)
    except (ValueError, TypeError):
        return False, "Invalid expiry"

    if exp_int != 0 and time.time() > exp_int:
        return False, "Link has expired. Check a recent email for a new link."

    expected = _sign(role, user_id, expiry)
    if not hmac.compare_digest(token, expected):
        return False, "Invalid token"

    return True, ""


def build_dashboard_footer_html() -> str:
    """Build the site-link footer appended to notification emails.

    Staff now sign in on the site itself (an emailed code, see routes/intake),
    so the footer no longer carries personal signed dashboard links. It is one
    button to the public site, identical for everyone, which means no Staff
    Directory read and no per-recipient token to build.

    Returns:
        The footer HTML with one Outlook-safe button to the site, or "" when no
        public site is configured (local development has DASHBOARD_FRONTEND_URL
        blank, so there is nowhere to point the button).
    """
    from app.services.welcome_email import site_links

    site_url, _ = site_links()                 # the one public address; "" in local dev
    if not site_url:                           # no site to link to: emit nothing
        return ""
    # Rendered from a template with the shared button macro, so the footer's
    # button survives classic Outlook like every other button in these emails.
    from app import templates_render
    return templates_render.render_dashboard_footer(site_url)


def _sign(role: str, user_id: str, expiry: str) -> str:
    payload = f"dashboard:{role}:{user_id}:{expiry}"
    return hmac.new(
        settings.APPROVAL_LINK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
