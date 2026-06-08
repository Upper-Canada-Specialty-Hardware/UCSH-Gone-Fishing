import hashlib
import hmac
import time

from app.config import settings

DEFAULT_EXPIRY_HOURS = 72


def generate_approval_url(
    request_type: str,
    request_id: str | int,
    action: str,
    manager_id: str | int,
    expiry_hours: int = DEFAULT_EXPIRY_HOURS,
    approval_version: int = 1,
) -> str:
    expiry = int(time.time()) + expiry_hours * 3600
    token = _sign(request_type, str(request_id), action, str(manager_id), str(expiry), approval_version)
    return (
        f"{settings.BASE_URL}/api/{request_type}/{action}/{request_id}"
        f"?token={token}&mgr={manager_id}&exp={expiry}&v={approval_version}"
    )


def validate_approval_token(
    request_type: str,
    request_id: str,
    action: str,
    manager_id: str,
    token: str,
    expiry: str,
    approval_version: int = 1,
) -> tuple[bool, str]:
    try:
        exp_int = int(expiry)
    except (ValueError, TypeError):
        return False, "Invalid expiry"

    if time.time() > exp_int:
        return False, "Link has expired"

    expected = _sign(request_type, request_id, action, manager_id, expiry, approval_version)
    if not hmac.compare_digest(token, expected):
        return False, "Invalid token"

    return True, ""


def _sign(
    request_type: str,
    request_id: str,
    action: str,
    manager_id: str,
    expiry: str,
    approval_version: int = 1,
) -> str:
    payload = f"{request_type}:{request_id}:{action}:{manager_id}:{expiry}"
    if approval_version and approval_version != 1:
        payload = f"{payload}:v{approval_version}"
    return hmac.new(
        settings.APPROVAL_LINK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
