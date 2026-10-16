"""The admin dashboard's password and signed-in sessions (#228).

The password lives in the one-row ``admin_password`` table. HR changes it
from the admin dashboard, which stores a salted PBKDF2 hash. A developer may
write any plain value into the table instead; it works as typed and is
replaced by its hash at the next sign-in. With no row, nobody can sign in.

Signing in hands the browser a session token: an expiry plus an HMAC over
that expiry and a fingerprint of the stored password. Every /admin API call
must carry it (``X-Admin-Session`` header), and any change to the stored
password, from the dashboard or the database, changes the fingerprint and so
ends every session at once.
"""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.admin_password import AdminPassword
from app.models.mixins import utcnow

HASH_PREFIX = "pbkdf2_sha256"            # marks a stored hash; anything else is plain text
HASH_ITERATIONS = 260_000                # slow enough to resist guessing a leaked hash
SESSION_DAYS = 30                        # matches the staff sign-in
SESSION_HEADER = "X-Admin-Session"
ADMIN_PATH = "/api/dashboard/admin/"     # every route this guards starts here
MAX_FAILURES = 5                         # wrong passwords allowed per IP ...
FAILURE_WINDOW_SECONDS = 15 * 60         # ... in this many seconds
MAX_PASSWORD_LENGTH = 200

NO_PASSWORD_MESSAGE = "No admin password is set. A developer needs to set one in the database."

# Recent wrong-password times per IP. In memory: one backend instance, and a
# restart clearing it only gives an attacker five more tries.
_failures: dict[str, deque] = defaultdict(deque)


class AdminAuthError(Exception):
    """A sign-in or password change that cannot go ahead; str() is readable."""

    def __init__(self, message: str, status_code: int = 400):
        """Keep the HTTP status the route should answer with.

        Args:
            message: Shown to the person.
            status_code: 400, 401, 403 or 429.
        """
        super().__init__(message)
        self.status_code = status_code


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash a password for storage.

    Args:
        password: The plain password.
        salt: Fixed salt for tests; a random one otherwise.

    Returns:
        "pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>".
    """
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, HASH_ITERATIONS)
    return f"{HASH_PREFIX}${HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def is_hashed(stored: str) -> bool:
    """Whether a stored value is a hash rather than plain text a developer typed."""
    return stored.startswith(HASH_PREFIX + "$")


def password_matches(password: str, stored: str) -> bool:
    """Check a typed password against the stored value, hash or plain.

    Args:
        password: What was typed.
        stored: The table's value.

    Returns:
        True on a match. Compared in constant time either way.
    """
    if not is_hashed(stored):
        return hmac.compare_digest(password.encode(), stored.encode())   # a developer's plain value
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    except ValueError:
        return False                                           # a mangled hash opens nothing
    return hmac.compare_digest(digest.hex(), digest_hex)


def _fingerprint(stored: str) -> str:
    """A short digest of the stored value; it changes whenever the password does."""
    return hashlib.sha256(stored.encode()).hexdigest()[:16]


def _sign(expiry: str, stored: str) -> str:
    """HMAC of a session's expiry, tied to the current stored password."""
    message = f"admin-session:{expiry}:{_fingerprint(stored)}"
    return hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_session(stored: str, now: float | None = None) -> dict:
    """A new session token for the current password.

    Args:
        stored: The table's value, after any rehash.
        now: Current time for tests.

    Returns:
        {"token": "<expiry>.<signature>", "exp": "<expiry>"}.
    """
    expiry = str(int((now or time.time()) + SESSION_DAYS * 86400))
    return {"token": f"{expiry}.{_sign(expiry, stored)}", "exp": expiry}


def session_is_valid(token: str, stored: str | None, now: float | None = None) -> bool:
    """Whether a session token is unexpired and signed for the current password.

    Args:
        token: "<expiry>.<signature>" from the header.
        stored: The table's value, or None when there is no row.
        now: Current time for tests.

    Returns:
        False for a missing, expired, forged or outdated token, or no password.
    """
    if not token or stored is None or "." not in token:
        return False
    expiry, signature = token.split(".", 1)
    if not expiry.isdigit() or int(expiry) < (now or time.time()):
        return False                                           # expired or malformed
    return hmac.compare_digest(signature, _sign(expiry, stored))


STREAM_TICKET_SECONDS = 120              # a live-feed ticket only has to survive the connect


def _sign_ticket(expiry: str, stored: str) -> str:
    """HMAC of a live-feed ticket's expiry, kept apart from sessions by its prefix."""
    message = f"events-ticket:{expiry}:{_fingerprint(stored)}"
    return hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_stream_ticket(stored: str, now: float | None = None) -> str:
    """A short-lived ticket that opens the admin live feed (#328).

    A browser's EventSource cannot send the X-Admin-Session header, so the feed
    takes a credential in its url, and urls end up in proxy logs. A ticket that
    dies in two minutes, and cannot be used as a session, keeps the 30-day
    session out of those logs.

    Args:
        stored: The table's password value (ties the ticket to it).
        now: Current time for tests.

    Returns:
        "<expiry>.<signature>".
    """
    expiry = str(int((now or time.time()) + STREAM_TICKET_SECONDS))
    return f"{expiry}.{_sign_ticket(expiry, stored)}"


def stream_ticket_is_valid(ticket: str, stored: str | None, now: float | None = None) -> bool:
    """Whether a live-feed ticket is unexpired and signed for the current password.

    Args:
        ticket: "<expiry>.<signature>" from the feed url.
        stored: The table's value, or None when there is no row.
        now: Current time for tests.

    Returns:
        False for a missing, expired, forged or outdated ticket.
    """
    if not ticket or stored is None or "." not in ticket:
        return False
    expiry, signature = ticket.split(".", 1)
    if not expiry.isdigit() or int(expiry) < (now or time.time()):
        return False                                           # expired or malformed
    return hmac.compare_digest(signature, _sign_ticket(expiry, stored))


async def _load_row(session) -> AdminPassword | None:
    """The password row, or None. The lowest id wins if a developer added more."""
    result = await session.execute(select(AdminPassword).order_by(AdminPassword.id).limit(1))
    return result.scalar_one_or_none()


async def stored_password() -> str | None:
    """The table's current value, or None when there is no row."""
    async with async_session() as session:
        row = await _load_row(session)
        return row.password if row else None


def _check_rate(ip: str, now: float) -> None:
    """Refuse an IP with too many recent wrong passwords.

    Raises:
        AdminAuthError: 429 while over the limit.
    """
    recent = _failures[ip]
    while recent and recent[0] < now - FAILURE_WINDOW_SECONDS:
        recent.popleft()                                       # drop tries outside the window
    if len(recent) >= MAX_FAILURES:
        raise AdminAuthError("Too many wrong passwords. Wait 15 minutes and try again.", 429)


async def sign_in(password: str, ip: str) -> dict:
    """Check the password and start a session.

    A developer's plain value is replaced by its hash on the way.

    Args:
        password: What was typed.
        ip: The caller, for the wrong-password limit.

    Returns:
        A session from make_session.

    Raises:
        AdminAuthError: 429 over the limit, 403 with no password set, 401 when wrong.
    """
    now = time.time()
    _check_rate(ip, now)
    async with async_session() as session:
        row = await _load_row(session)
        if row is None:
            raise AdminAuthError(NO_PASSWORD_MESSAGE, 403)
        if not password_matches(password, row.password):
            _failures[ip].append(now)                          # counts toward the limit
            raise AdminAuthError("That password is not right.", 401)
        if not is_hashed(row.password):
            row.password = hash_password(password)             # never keep plain text past one sign-in
            row.updated_at = utcnow()
            await session.commit()
        _failures.pop(ip, None)                                # a right password clears the count
        return make_session(row.password, now)


async def change_password(current: str, new: str) -> dict:
    """Replace the password, which ends every other session.

    Args:
        current: The password now, checked again before changing it.
        new: The new one.

    Returns:
        A fresh session for the device that changed it.

    Raises:
        AdminAuthError: 400 for an empty or overlong new password or a wrong
            current one (not 401, which the page reads as signed out), 403
            with no password set.
    """
    new = new.strip()
    if not new:
        raise AdminAuthError("Enter a new password.")
    if len(new) > MAX_PASSWORD_LENGTH:
        raise AdminAuthError(f"Keep the password under {MAX_PASSWORD_LENGTH} characters.")
    async with async_session() as session:
        row = await _load_row(session)
        if row is None:
            raise AdminAuthError(NO_PASSWORD_MESSAGE, 403)
        if not password_matches(current, row.password):
            raise AdminAuthError("The current password is not right.")
        row.password = hash_password(new)
        row.updated_at = utcnow()
        await session.commit()
        return make_session(row.password)


async def admin_guard(request: Request) -> None:
    """Refuse an /admin API call without a valid admin session.

    Added as a router dependency in main.py to every router mounted at
    /api/dashboard, so it runs for all of them but only checks paths under
    ADMIN_PATH. Staff (/me) and manager (/team) calls keep their own checks.

    Args:
        request: The incoming request.

    Raises:
        HTTPException: 401 when the session is missing, expired, or from
            before the password last changed.
    """
    if not request.url.path.startswith(ADMIN_PATH):
        return                                                 # not an admin call
    token = request.headers.get(SESSION_HEADER, "")
    if not session_is_valid(token, await stored_password()):
        raise HTTPException(status_code=401, detail="Sign in to the admin dashboard.")
