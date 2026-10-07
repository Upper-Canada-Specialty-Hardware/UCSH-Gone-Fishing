"""One-time email codes for the public request page.

A person proves they own an email address by typing back a 6-digit code sent to
it. This module issues and checks those codes, and mints the short "verified
email" token handed to someone whose address is not in the Staff Directory yet.

Split like the rest of the services: pure helpers (no I/O, easy to test) and
thin async functions that read and write the email_codes table.

Rules (approved for #131):
* every address gets a code, known or not, so the page never reveals who is on
  staff;
* a code lasts CODE_TTL_MINUTES and allows MAX_ATTEMPTS wrong guesses;
* at most MAX_CODES_PER_EMAIL codes per address and MAX_CODES_PER_IP per caller
  inside RATE_WINDOW_MINUTES;
* only an HMAC of the code is stored, and only the newest code for an address
  works.
"""

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.config import settings
from app.database import async_session
from app.models import EmailCode
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)

CODE_TTL_MINUTES = 10          # how long a sent code stays usable
MAX_ATTEMPTS = 5               # wrong guesses allowed per code
RATE_WINDOW_MINUTES = 15       # window both send limits are counted over
MAX_CODES_PER_EMAIL = 3        # codes one address can be sent per window
MAX_CODES_PER_IP = 10          # codes one caller can request per window
VERIFIED_EMAIL_TTL_HOURS = 24  # how long an unknown person has to finish the form


# ----- pure helpers -----

def normalise_email(email: str) -> str:
    """Trim and lowercase an address so lookups and limits match.

    Args:
        email: The address as typed.

    Returns:
        The canonical form used everywhere in this module.
    """
    return (email or "").strip().lower()


def generate_code() -> str:
    """Make a random 6-digit code, keeping leading zeros.

    Returns:
        A string such as "042917".
    """
    return f"{secrets.randbelow(1_000_000):06d}"  # cryptographic randomness, zero-padded


def hash_code(email: str, code: str) -> str:
    """HMAC a code together with its address, so a stored hash is useless alone.

    Args:
        email: The normalised address the code belongs to.
        code: The 6-digit code.

    Returns:
        Hex SHA-256 HMAC keyed with APPROVAL_LINK_SECRET.
    """
    message = f"{email}:{code}".encode()                      # binding to the email stops reuse across addresses
    return hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC.

    SQLite (tests, local dev) hands timestamps back without a zone; Postgres
    keeps it. Comparing the two kinds raises, so everything is made aware first.

    Args:
        value: A datetime from the database or the clock.

    Returns:
        The same moment, timezone-aware.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_expired(created_at: datetime, now: datetime) -> bool:
    """Whether a code sent at ``created_at`` is past its lifetime at ``now``.

    Args:
        created_at: When the code was sent.
        now: The current time.

    Returns:
        True once CODE_TTL_MINUTES have passed.
    """
    return as_utc(now) - as_utc(created_at) > timedelta(minutes=CODE_TTL_MINUTES)


def sign_verified_email(email: str, expiry: int | None = None) -> dict:
    """Mint the token proving an address was verified, for someone not on staff.

    Same HMAC scheme as the dashboard tokens, under its own prefix so it can
    never be mistaken for a dashboard token.

    Args:
        email: The normalised, verified address.
        expiry: Unix expiry; defaults to VERIFIED_EMAIL_TTL_HOURS from now.

    Returns:
        {"email", "exp", "token"} for the client to send back with the form.
    """
    exp = expiry if expiry is not None else int(time.time()) + VERIFIED_EMAIL_TTL_HOURS * 3600
    message = f"verified-email:{email}:{exp}".encode()
    token = hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()
    return {"email": email, "exp": str(exp), "token": token}


def check_verified_email(email: str, expiry: str, token: str) -> bool:
    """Validate a token from sign_verified_email.

    Args:
        email: The address the token claims.
        expiry: Its unix expiry, as sent back by the client.
        token: The HMAC.

    Returns:
        True only for an unexpired token minted for exactly this address.
    """
    try:
        exp = int(expiry)
    except (TypeError, ValueError):
        return False                                           # malformed expiry
    if time.time() > exp:
        return False                                           # past its lifetime
    expected = sign_verified_email(normalise_email(email), exp)["token"]
    return hmac.compare_digest(token or "", expected)          # constant-time compare


# ----- database-backed -----

@dataclass
class IssueResult:
    """What happened when a code was requested.

    Attributes:
        code: The plain code to email, or None when nothing should be sent.
        reason: "issued", "email_limit" or "ip_limit".
    """
    code: str | None
    reason: str


async def issue_code(email: str, requester_ip: str) -> IssueResult:
    """Create a code for an address, unless a send limit has been reached.

    Args:
        email: The address as typed; normalised here.
        requester_ip: The caller's IP, for the per-IP limit.

    Returns:
        IssueResult with the plain code to send, or the limit that stopped it.
    """
    email = normalise_email(email)
    now = utcnow()
    window_start = now - timedelta(minutes=RATE_WINDOW_MINUTES)

    async with async_session() as session:
        # Per-address limit: stops anyone flooding one inbox.
        sent_to_email = await session.scalar(
            select(func.count()).select_from(EmailCode)
            .where(EmailCode.email == email, EmailCode.created_at >= window_start)
        )
        if sent_to_email >= MAX_CODES_PER_EMAIL:
            return IssueResult(None, "email_limit")

        # Per-caller limit: stops one source spraying many addresses.
        sent_from_ip = await session.scalar(
            select(func.count()).select_from(EmailCode)
            .where(EmailCode.requester_ip == requester_ip, EmailCode.created_at >= window_start)
        )
        if sent_from_ip >= MAX_CODES_PER_IP:
            return IssueResult(None, "ip_limit")

        code = generate_code()
        session.add(EmailCode(
            email=email,
            code_hash=hash_code(email, code),                  # never the code itself
            requester_ip=requester_ip,
            created_at=now,
            attempts=0,
        ))
        await session.commit()

    return IssueResult(code, "issued")


@dataclass
class CheckResult:
    """The outcome of checking a typed code.

    Attributes:
        ok: True when the code was right and is now used up.
        error: Why it failed, worded for the person: None when ok.
    """
    ok: bool
    error: str | None = None


async def check_code(email: str, code: str) -> CheckResult:
    """Check a typed code against the newest one sent to the address.

    Every try, right or wrong, first takes one of the code's MAX_ATTEMPTS
    slots with a single conditional UPDATE, and a right code is then used up
    with another. Both are done in the database, not in Python, so guesses
    sent in parallel cannot get past the limit and a code cannot be used twice.

    Args:
        email: The address as typed; normalised here.
        code: The 6 digits the person entered.

    Returns:
        CheckResult saying whether the address is now verified.
    """
    email = normalise_email(email)
    code = (code or "").strip()
    now = utcnow()

    async with async_session() as session:
        # Only the newest code counts: asking again retires the older one.
        row = await session.scalar(
            select(EmailCode)
            .where(EmailCode.email == email)
            .order_by(EmailCode.created_at.desc(), EmailCode.id.desc())
            .limit(1)
        )
        if row is None or row.consumed_at is not None:
            return CheckResult(False, "No code is waiting for this email. Ask for a new one.")
        if is_expired(row.created_at, now):
            return CheckResult(False, "This code has expired. Ask for a new one.")

        # Take one try slot; only succeeds while unused and under the limit.
        taken = await session.execute(
            update(EmailCode)
            .where(EmailCode.id == row.id,
                   EmailCode.attempts < MAX_ATTEMPTS,
                   EmailCode.consumed_at.is_(None))
            .values(attempts=EmailCode.attempts + 1)           # incremented in SQL, not Python
        )
        await session.commit()
        if taken.rowcount != 1:                                # limit reached, or used meanwhile
            return CheckResult(False, "Too many wrong tries. Ask for a new code.")

        if not hmac.compare_digest(row.code_hash, hash_code(email, code)):
            attempts = await session.scalar(                   # fresh count, parallel tries included
                select(EmailCode.attempts).where(EmailCode.id == row.id)
            )
            left = MAX_ATTEMPTS - attempts
            if left <= 0:
                return CheckResult(False, "Too many wrong tries. Ask for a new code.")
            return CheckResult(False, f"That code is not right. {left} tries left.")

        # Use the code up; only the first of two parallel right answers wins.
        used = await session.execute(
            update(EmailCode)
            .where(EmailCode.id == row.id, EmailCode.consumed_at.is_(None))
            .values(consumed_at=now)
        )
        await session.commit()
        if used.rowcount != 1:
            return CheckResult(False, "No code is waiting for this email. Ask for a new one.")

    logger.info("Email code verified")                         # address not logged
    return CheckResult(True)
