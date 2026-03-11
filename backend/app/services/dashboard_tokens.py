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


async def build_dashboard_links(employee_id: str | int) -> list[dict]:
    """Build dashboard link dicts for an employee based on their roles."""
    if not settings.DASHBOARD_FRONTEND_URL:
        return []

    from app.services.employee import get_employee_by_id, ADMIN_NAMES, is_manager

    emp = await get_employee_by_id(employee_id)
    if not emp:
        return []

    fields = emp.get("fields", {})
    name = fields.get("Title", "")

    links = [{"label": "My Dashboard", "url": generate_dashboard_url("employee", employee_id)}]

    if await is_manager(name):
        links.append({"label": "Team Dashboard", "url": generate_dashboard_url("manager", employee_id)})

    if name in ADMIN_NAMES:
        links.append({"label": "Admin Dashboard", "url": generate_dashboard_url("admin", employee_id)})

    return links


async def build_dashboard_footer_html(employee_id: str | int) -> str:
    """Generate dashboard footer HTML for an employee."""
    links = await build_dashboard_links(employee_id)
    if not links:
        return ""

    link_buttons = " ".join(
        f'<a href="{link["url"]}" style="display:inline-block;padding:8px 20px;background:#1e40af;'
        f'color:white;text-decoration:none;border-radius:4px;font-size:13px;margin-right:8px;'
        f'margin-bottom:4px;">{link["label"]}</a>'
        for link in links
    )
    return (
        f'<div style="margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;'
        f'font-family:\'Segoe UI\',sans-serif;">'
        f'<p style="color:#6b7280;font-size:13px;margin-bottom:8px;">View your dashboards:</p>'
        f'{link_buttons}</div>'
    )


def _sign(role: str, user_id: str, expiry: str) -> str:
    payload = f"dashboard:{role}:{user_id}:{expiry}"
    return hmac.new(
        settings.APPROVAL_LINK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
