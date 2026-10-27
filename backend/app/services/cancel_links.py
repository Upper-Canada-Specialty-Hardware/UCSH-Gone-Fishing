"""The signed "Cancel this request" link in a staff member's receipt email (#519).

The link carries the request's kind, number and the staff id it belongs to,
signed with the approval-link secret, so only the person who got the receipt can
use it and nobody can forge one for someone else's request. It points at the
public site (``#/cancel?token=...``), which shows the request and a confirm
button; opening the link alone changes nothing, so a mail scanner that follows
links cannot cancel anything. Only a pending request can be cancelled; the page
says what happened to one that has moved on.

Kept apart from ``own_requests`` so the receipt builder in ``request_submission``
can add the link without importing the cancel service (which imports it).
"""

import hashlib
import hmac

from app.config import settings
from app.models import request_vocab as vocab

# A kind's url spelling -> the stored one, so either can be passed in.
_KIND_SPELLING = {"carryover-payout": "carryover_payout"}      # the url form -> the stored kind


def cancel_token(kind: str, item_id, employee_id) -> str:
    """The signed token for one person's cancel link to one request.

    Args:
        kind: The stored kind ("leave", "overtime", "carryover_payout").
        item_id: The request number.
        employee_id: The staff id the request belongs to.

    Returns:
        ``"<kind>.<item_id>.<employee_id>.<signature>"``.
    """
    message = f"own-cancel:{kind}:{item_id}:{employee_id}".encode()   # every part, kept apart by a prefix
    signature = hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    return f"{kind}.{item_id}.{employee_id}.{signature}"


def decode_cancel_token(token: str) -> tuple[str, int, str] | None:
    """The ``(kind, item_id, employee_id)`` a cancel token names, when valid.

    Args:
        token: A token from ``cancel_token``.

    Returns:
        The three parts, or None for a malformed or forged token.
    """
    parts = (token or "").split(".")                           # "<kind>.<item>.<staff id>.<sig>"
    if len(parts) != 4 or parts[0] not in vocab.KINDS or not parts[1].isdigit():
        return None                                            # not a token at all
    kind, item_id, employee_id, _ = parts
    expected = cancel_token(kind, int(item_id), employee_id)    # re-sign and compare in constant time
    return (kind, int(item_id), employee_id) if hmac.compare_digest(token, expected) else None


def cancel_link(kind: str, item_id, employee_id) -> str:
    """The full "Cancel this request" URL, on the public site.

    Args:
        kind: The stored kind, or its url form ("carryover-payout").
        item_id: The request number.
        employee_id: The staff id the request belongs to.

    Returns:
        A signed URL on ``DASHBOARD_FRONTEND_URL``.
    """
    stored = _KIND_SPELLING.get(kind, kind)                    # accept either spelling
    base = settings.DASHBOARD_FRONTEND_URL.rstrip("/")         # the public site, as the other emailed links
    return f"{base}/#/cancel?token={cancel_token(stored, item_id, employee_id)}"


def cancel_link_html(kind: str, item_id, employee_id) -> str:
    """The receipt's closing paragraph with the cancel link.

    Args:
        kind: The stored kind.
        item_id: The request number.
        employee_id: The staff id the request belongs to.

    Returns:
        An HTML paragraph, or "" when the request has no staff id to sign.
    """
    if not employee_id:
        return ""                                              # nothing to sign the link for
    url = cancel_link(kind, item_id, employee_id)
    return (
        '<p style="font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.5;'
        'color:#1f1a14;margin:16px 0 0;">Changed your mind? While it is still waiting for a '
        f'decision you can <a href="{url}" style="color:#2f5d50;font-weight:bold;">cancel this request</a>.</p>'
    )
