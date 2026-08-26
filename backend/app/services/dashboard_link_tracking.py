"""Record and query when each person's dashboard link expires.

Dashboard links are minted only while sending an email, and they last 30 days.
Whether someone keeps access therefore depends entirely on whether email happens
to reach them — which is why a manager on a busy team never expires and a quiet
employee silently does. This module is the record that makes the difference
visible, so the renewal task can act on it.

Everything here swallows its own failures. It is bookkeeping attached to the
sending of real email, and a failure to write a row must never turn a delivered
notification into an error for the caller.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import async_session
from app.models import DashboardLinkState
from app.models.mixins import utcnow
from app.services.dashboard_tokens import DEFAULT_EXPIRY_DAYS

logger = logging.getLogger(__name__)


async def record_link_sent(employee_id: str | int, expiry_days: int = DEFAULT_EXPIRY_DAYS) -> None:
    """Note that this person has just been emailed a working dashboard link.

    Call only once the email has actually been accepted for delivery. An
    attempted send is not a delivered link — the dashboard footer is skipped
    when the employee lookup fails and swallowed on any exception, so recording
    earlier would mark people as covered who received nothing.

    Args:
        employee_id: Staff Directory item id the link was minted for. This is
            the recipient of the email, which on an approval request is the
            manager rather than the employee the request is about.
        expiry_days: Lifetime of the link that was sent, defaulting to the same
            constant the minting code uses.
    """
    now = utcnow()
    expires_at = now + timedelta(days=expiry_days)
    try:
        async with async_session() as session:
            row = await session.get(DashboardLinkState, str(employee_id))
            if row is None:
                session.add(DashboardLinkState(
                    employee_id=str(employee_id),
                    last_sent_at=now,
                    expires_at=expires_at,
                ))
            else:
                row.last_sent_at = now
                row.expires_at = expires_at
            await session.commit()
    except Exception:  # noqa: BLE001 - bookkeeping must not fail a sent email
        logger.exception("Could not record the dashboard link sent to employee #%s", employee_id)


async def find_expiring(before: datetime) -> list[str]:
    """List the people whose current link stops working before a given moment.

    Args:
        before: Cut-off — rows expiring at or after this are left alone.

    Returns:
        Staff Directory item ids, oldest expiry first so the most urgent are
        renewed before any per-run limit is reached.
    """
    async with async_session() as session:
        result = await session.execute(
            select(DashboardLinkState.employee_id)
            .where(DashboardLinkState.expires_at <= before)
            .order_by(DashboardLinkState.expires_at)
        )
        return [row[0] for row in result]


async def known_employee_ids() -> set[str]:
    """Every employee already tracked, so the seeder can find who is missing."""
    async with async_session() as session:
        result = await session.execute(select(DashboardLinkState.employee_id))
        return {row[0] for row in result}


async def seed_missing(employee_ids: list[str], spread_days: int) -> int:
    """Start the clock for people who have never been tracked, without emailing.

    Someone who has never received a link-bearing email has no row, so the
    renewal query cannot see them — including anyone whose link already lapsed
    before this table existed, who are exactly the people the feature is for.
    Seeding puts them in scope.

    Expiries are spread across `spread_days` rather than set to now, because
    making everyone due at once would mean emailing the entire company through
    a ten-per-minute rate limit on the first run. Spread, roughly a thirtieth of
    people come due each day and renewal settles into a steady trickle.

    The offset is derived from the employee id so it is stable: re-running never
    moves anyone to a different day.

    Args:
        employee_ids: Everyone who should be tracked.
        spread_days: Window to distribute the seeded expiries over.

    Returns:
        How many rows were created.
    """
    existing = await known_employee_ids()
    missing = [str(e) for e in employee_ids if str(e) not in existing]
    if not missing:
        return 0

    now = utcnow()
    try:
        async with async_session() as session:
            for employee_id in missing:
                session.add(DashboardLinkState(
                    employee_id=employee_id,
                    last_sent_at=now,
                    expires_at=now + timedelta(days=_seed_offset(employee_id, spread_days)),
                ))
            await session.commit()
    except Exception:  # noqa: BLE001 - a failed seed retries on the next run
        logger.exception("Could not seed dashboard link state for %d employee(s)", len(missing))
        return 0

    logger.info("Seeded dashboard link state for %d employee(s)", len(missing))
    return len(missing)


def _seed_offset(employee_id: str, spread_days: int) -> int:
    """Pick a stable day offset within the spread window for one employee.

    Uses the numeric id where there is one, so consecutive employees land on
    consecutive days and the load spreads evenly. Anything unparseable falls to
    the first day rather than raising — being renewed early is harmless.
    """
    try:
        return int(employee_id) % spread_days
    except (TypeError, ValueError):
        return 0
