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
) -> str:
    expiry = int(time.time()) + expiry_hours * 3600
    token = _sign(request_type, str(request_id), action, str(manager_id), str(expiry))
    return (
        f"{settings.BASE_URL}/api/{request_type}/{action}/{request_id}"
        f"?token={token}&mgr={manager_id}&exp={expiry}"
    )


def validate_approval_token(
    request_type: str,
    request_id: str,
    action: str,
    manager_id: str,
    token: str,
    expiry: str,
) -> tuple[bool, str]:
    # Check expiry
    try:
        exp_int = int(expiry)
    except (ValueError, TypeError):
        return False, "Invalid expiry"

    if time.time() > exp_int:
        return False, "Link has expired"

    expected = _sign(request_type, request_id, action, manager_id, expiry)
    if not hmac.compare_digest(token, expected):
        return False, "Invalid token"

    return True, ""


def _sign(request_type: str, request_id: str, action: str, manager_id: str, expiry: str) -> str:
    payload = f"{request_type}:{request_id}:{action}:{manager_id}:{expiry}"
    return hmac.new(
        settings.APPROVAL_LINK_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
