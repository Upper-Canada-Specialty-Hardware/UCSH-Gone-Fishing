"""Persist and query every outbound email attempt.

The email client in ``app/graph/email.py`` calls ``record_email`` once per
attempt, whatever the outcome, and the admin email-log endpoint calls
``find_emails`` to answer "what did we send this person, and did SMTP2GO
accept it". Everything here is bookkeeping attached to the sending of real
email: a failure to write or read a row is logged and swallowed, and must
never turn a delivered notification into an error for the caller.

Addresses are normalised on the way in and on the way out so that a Staff
Directory value with trailing whitespace or mixed case (both real cases)
still matches the address an admin types into the lookup.
"""

import logging
from datetime import datetime

from sqlalchemy import desc, or_, select

from app.database import async_session
from app.models import EmailLog

logger = logging.getLogger(__name__)

# Outcome of one attempt, stored in EmailLog.status.
STATUS_SENT = "sent"        # SMTP2GO accepted every recipient
STATUS_PARTIAL = "partial"  # SMTP2GO accepted some recipients and rejected others
STATUS_FAILED = "failed"    # HTTP error, network error, or every recipient rejected
STATUS_SKIPPED = "skipped"  # SMTP2GO was never called (no usable recipient address)

# Error text is for a human reading the log, not a full response archive.
ERROR_MAX_CHARS = 500

# How far back the admin lookup reaches by default. Thirty days matches the
# dashboard link lifetime and the first reminder interval, so one lookup
# covers a full notification cycle. Rows are kept beyond this; only the
# default view is bounded, and a caller can widen it with the days parameter.
DEFAULT_WINDOW_DAYS = 30


def normalize_address(address: str | None) -> str:
    """Lower-case and strip an email address so stored and queried forms agree.

    Args:
        address: Raw address as found in the Staff Directory or typed by an
            admin. May be None; may carry surrounding whitespace.

    Returns:
        The normalised address, or "" when the input is None or blank.
    """
    return (address or "").strip().lower()


def pack_addresses(addresses: list[str] | None) -> str:
    """Join a recipient list into the delimited form the table stores.

    Args:
        addresses: Recipient list as passed to the email client. None entries
            and blanks are dropped, the rest normalised.

    Returns:
        ",a@x.com,b@x.com," with every address wrapped by commas, so a
        LIKE '%,a@x.com,%' match is exact per address. "" when nothing is left.
    """
    cleaned = [normalize_address(a) for a in (addresses or [])]  # normalise each
    cleaned = [a for a in cleaned if a]                            # drop blanks
    return "," + ",".join(cleaned) + "," if cleaned else ""        # wrap for exact LIKE


def unpack_addresses(packed: str | None) -> list[str]:
    """Reverse ``pack_addresses`` for API output.

    Args:
        packed: The stored ",a@x.com,b@x.com," string, or None.

    Returns:
        The address list, empty for None or "".
    """
    return [a for a in (packed or "").split(",") if a]


def truncate_error(text: str | None) -> str | None:
    """Cap error text at ``ERROR_MAX_CHARS`` so a huge response body cannot bloat a row.

    Args:
        text: Error text, or None.

    Returns:
        The text cut to the cap, or None when there was none.
    """
    if not text:
        return None
    return text[:ERROR_MAX_CHARS]


async def record_email(
    *,
    status: str,
    to: list[str] | None,
    subject: str,
    cc: list[str] | None = None,
    primary_employee_id: str | int | None = None,
    smtp2go_email_id: str | None = None,
    smtp2go_request_id: str | None = None,
    http_status: int | None = None,
    error: str | None = None,
) -> None:
    """Write one EmailLog row describing an attempt. Never raises.

    Args:
        status: One of the STATUS_* constants.
        to: Recipient list as the caller supplied it (unnormalised is fine).
        subject: Email subject, stored verbatim.
        cc: Optional CC list.
        primary_employee_id: Staff Directory id of the main recipient, when
            the caller knew it. Stored as a string to match the dashboard uid.
        smtp2go_email_id: SMTP2GO's ``data.email_id`` for an accepted message.
        smtp2go_request_id: SMTP2GO's top-level ``request_id``.
        http_status: HTTP status SMTP2GO answered with, if it answered.
        error: Human-readable reason for a failed or skipped attempt.
    """
    try:
        async with async_session() as session:
            session.add(EmailLog(
                status=status,
                to_addresses=pack_addresses(to),
                cc_addresses=pack_addresses(cc) or None,            # "" -> NULL when no CC
                subject=subject or "",
                primary_employee_id=(
                    str(primary_employee_id) if primary_employee_id else None
                ),
                smtp2go_email_id=smtp2go_email_id,
                smtp2go_request_id=smtp2go_request_id,
                http_status=http_status,
                error=truncate_error(error),
            ))
            await session.commit()
    except Exception:
        # Bookkeeping only: the email outcome has already been decided and the
        # caller must see that outcome, not a database problem.
        logger.exception("Could not record email log row for subject %r", subject)


async def find_emails(
    *,
    employee_id: str | int | None = None,
    address: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[EmailLog]:
    """Newest-first email attempts involving a person.

    A row matches when its primary employee id equals ``employee_id`` OR the
    address appears in its To or CC list. Both are checked because the id is
    only recorded for sends that carried a dashboard footer, while a
    confirmation copied to a manager reaches them by address alone.

    Args:
        employee_id: Staff Directory item id to match against the primary id.
        address: Email address to match in To/CC; normalised here.
        since: Only rows sent at or after this instant, when given.
        limit: Maximum rows to return.

    Returns:
        Matching EmailLog rows, newest first. Empty when neither an id nor an
        address was given, or when the query itself fails.
    """
    conditions = []
    if employee_id:
        conditions.append(EmailLog.primary_employee_id == str(employee_id))  # id match
    needle = normalize_address(address)
    if needle:
        pattern = "%," + needle + ",%"                                        # exact-per-address LIKE
        conditions.append(EmailLog.to_addresses.like(pattern))
        conditions.append(EmailLog.cc_addresses.like(pattern))
    if not conditions:
        return []                                                             # nothing to match on

    stmt = select(EmailLog).where(or_(*conditions))
    if since is not None:
        stmt = stmt.where(EmailLog.sent_at >= since)                          # window filter
    stmt = stmt.order_by(desc(EmailLog.sent_at), desc(EmailLog.id)).limit(limit)

    try:
        async with async_session() as session:
            return list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.exception("Email log query failed for employee %s / %s", employee_id, needle)
        return []
