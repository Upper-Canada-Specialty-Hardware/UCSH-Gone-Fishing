"""A short, app-wide pause on request writes while the storage switch runs (#335).

The flip from SharePoint to Postgres runs a final sync and then changes the
``requests_storage`` setting. If a manager approved, rejected or refunded a
request, or someone submitted a new one, or a staff record was edited, during
that final sync, the write would land on the old backend and be lost (or be
applied twice when re-driven from the new one). To close that window the flip
pauses those writes for its short duration.

The pause is a database setting, not a process flag, so every running instance
sees it at once (the switch may run on one instance while a manager approves on
another). It carries a safety expiry: if the process that set it crashes before
clearing it, the pause lapses on its own after ``PAUSE_MAX`` so the app is never
left stuck closed. ``begin_pause`` sets it right before the final sync and
``end_pause`` clears it in the flip's ``finally`` after the settings flip.

What the pause refuses: approvals, rejections, refunds, new submissions (the
forms routes and the dashboard, email-link and SMS decision entry points) and
staff writes through ``staff_store``. Reads, background reminders and the outbox
keep running; they do not move a balance or create a request.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.services.app_settings import SWITCH_PAUSE, get_setting, set_setting

logger = logging.getLogger(__name__)

# The sentence shown to anyone whose write is refused during the switch. Plain,
# and the same on every channel (an API 503 body and the SMS reply alike).
PAUSE_MESSAGE = "The system is being updated. Please try again in a minute."

# The longest a pause can last before it lapses on its own. The flip takes
# seconds; this is only a safety net so a crash mid-flip never leaves the app
# paused forever. Fifteen minutes is far longer than any real final sync.
PAUSE_MAX = timedelta(minutes=15)


class SystemPaused(Exception):
    """A write was attempted while the storage switch holds the pause.

    Attributes:
        message: The friendly sentence a route turns into its 503 body or SMS
            reply.
    """

    def __init__(self, message: str = PAUSE_MESSAGE):
        """Build the error with the friendly pause message.

        Args:
            message: The sentence to show the caller; the default pause message.
        """
        super().__init__(message)
        self.message = message                                  # the route reads this for its response


def _parse_until(value) -> datetime | None:
    """Read the stored ``until`` timestamp back into an aware UTC datetime.

    Args:
        value: The stored setting ({"until": "<ISO>"}), or anything falsy.

    Returns:
        The aware UTC expiry, or None when nothing usable is stored.
    """
    if not isinstance(value, dict):
        return None                                             # never set, or cleared to None
    raw = value.get("until")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))               # the ISO string begin_pause wrote
    except ValueError:
        logger.warning("Ignoring an unreadable switch pause: %r", value)
        return None                                             # unreadable counts as not paused
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)   # treat a naive value as UTC


async def begin_pause(now: datetime | None = None) -> datetime:
    """Start the pause, with an expiry ``PAUSE_MAX`` from now.

    Args:
        now: The moment to measure the expiry from; defaults to now in UTC.

    Returns:
        The expiry timestamp written.
    """
    until = (now or datetime.now(timezone.utc)) + PAUSE_MAX     # when the pause lapses on its own
    await set_setting(SWITCH_PAUSE, {"until": until.isoformat()})   # one row every instance reads
    logger.info("Switch pause started; it lapses by %s", until.isoformat())
    return until


async def end_pause() -> None:
    """Clear the pause so writes resume. Safe to call when not paused."""
    await set_setting(SWITCH_PAUSE, None)                       # None means the app is open again
    logger.info("Switch pause cleared")


async def is_paused(now: datetime | None = None) -> bool:
    """Whether a write should be refused right now.

    Reads the setting from the database on every call, so a pause set on one
    instance is seen on every other. An expired pause (past its ``until``) reads
    as not paused, which is the safety net against a crash mid-flip.

    Args:
        now: The moment to compare against the expiry; defaults to now in UTC.

    Returns:
        True while the pause is set and has not lapsed.
    """
    until = _parse_until(await get_setting(SWITCH_PAUSE))       # the stored expiry, or None
    if until is None:
        return False                                            # no pause set, or it was unreadable
    return (now or datetime.now(timezone.utc)) < until          # still inside the window


async def raise_if_paused(now: datetime | None = None) -> None:
    """Raise ``SystemPaused`` when a write is attempted during the switch.

    The one call every guarded write makes at its top. Routes translate the
    exception into a 503 (API) or the friendly SMS reply; a background caller
    lets it propagate and is retried once the short pause clears.

    Args:
        now: The moment to test; defaults to now in UTC.

    Raises:
        SystemPaused: While the pause is set and has not lapsed.
    """
    if await is_paused(now):
        raise SystemPaused()                                    # carries PAUSE_MESSAGE for the caller
