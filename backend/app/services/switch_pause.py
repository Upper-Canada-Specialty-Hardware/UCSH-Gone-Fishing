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
import asyncio
import functools
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
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


# ----- in-flight tracking, so the switch waits for writes that started before it -----
#
# ``raise_if_paused`` only refuses a write that starts DURING the pause. A write
# that passed its check a moment before ``begin_pause`` (a Graph-throttled approve,
# say) is still running when the final sync reads SharePoint, so its balance write
# can land after the read and be lost. ``guarded_write`` closes that window: it
# counts the operation as in flight for as long as it runs, and the switch waits
# for the count to reach zero before it reads. The app runs as one backend
# instance, so a plain in-process counter sees every guarded write; the event loop
# is single-threaded, so ``_in_flight += 1`` and the switch's read of it never tear.

_in_flight = 0                                                  # guarded writes running right now

# The backend each store should use for the length of a guarded write, captured at
# guard entry so a decision that started on SharePoint finishes on SharePoint even
# if the setting flips underneath it. None means "no guard active; read the setting".
_pin_requests: ContextVar[bool | None] = ContextVar("switch_pin_requests", default=None)
_pin_staff: ContextVar[bool | None] = ContextVar("switch_pin_staff", default=None)


def in_flight_count() -> int:
    """How many guarded writes are running right now.

    Returns:
        The current in-flight count; the switch waits for this to reach zero.
    """
    return _in_flight


def pinned_requests_backend() -> bool | None:
    """The request backend pinned for the current guarded write, if any.

    Returns:
        True (Postgres) or False (SharePoint) while a guarded write holds the
        pin, else None so the store reads the live setting.
    """
    return _pin_requests.get()


def pinned_staff_backend() -> bool | None:
    """The staff backend pinned for the current guarded write, if any.

    Returns:
        True (Postgres) or False (SharePoint) while a guarded write holds the
        pin, else None so the store reads the live setting.
    """
    return _pin_staff.get()


async def _capture_pins() -> tuple:
    """Pin both stores to their current backend for this operation.

    Imported lazily, because the stores import this module, so a top-level import
    would be a cycle.

    Returns:
        The two ContextVar tokens to reset when the guard exits.
    """
    from app.repositories.request_store import requests_in_postgres   # lazy: avoid an import cycle
    from app.repositories.staff_store import staff_in_postgres        # lazy: avoid an import cycle
    req = await requests_in_postgres()                         # the backend requests live in now
    stf = await staff_in_postgres()                            # the backend staff live in now
    return _pin_requests.set(req), _pin_staff.set(stf)         # tokens the finally resets


@asynccontextmanager
async def guarded_write(now: datetime | None = None):
    """Refuse a write while paused; otherwise count it in flight until it exits.

    The one wrapper a balance-moving write uses instead of ``raise_if_paused``: it
    makes the same refusal, and it also keeps the operation counted so the switch
    waits for it before the final sync reads. The count is bumped BEFORE the pause
    is re-checked, so the switch can never read zero while this write is between
    its check and its write.

    Args:
        now: The moment to test the pause against; defaults to now in UTC.

    Raises:
        SystemPaused: While the pause is set and has not lapsed.
    """
    global _in_flight
    _in_flight += 1                                            # count first, so the switch never reads past us
    # Re-entrant: a guarded write nested inside another (a staff store write made
    # by a decision) is already counted and pinned, so it must not re-check the
    # pause; a pause that began mid-decision would otherwise half-finish it.
    nested = _pin_requests.get() is not None
    try:
        if not nested and await is_paused(now):               # the switch started: back out and refuse
            raise SystemPaused()
        tokens = await _capture_pins()                        # pin the backends for this operation
    except BaseException:
        _in_flight -= 1                                       # nothing is in flight if the guard did not open
        raise
    tok_r, tok_s = tokens
    try:
        yield                                                 # the caller does its read-modify-write here
    finally:
        _pin_requests.reset(tok_r)                            # drop the pins
        _pin_staff.reset(tok_s)
        _in_flight -= 1                                       # the write is done; the switch may proceed


def guarded(fn):
    """Run a whole async write function inside ``guarded_write``.

    For the request create, approve, reject and refund entry points: the pause
    is checked once on entry and the call stays counted in flight until it
    returns, so the switch waits for it before the final sync reads.

    Args:
        fn: The async function to wrap.

    Returns:
        The wrapped async function, with the same signature and docstring.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        async with guarded_write():                           # refuse mid-switch; else count in flight
            return await fn(*args, **kwargs)
    return wrapper


async def wait_until_idle(timeout: float, poll: float = 0.1) -> bool:
    """Wait for every in-flight guarded write to finish, up to ``timeout`` seconds.

    Args:
        timeout: The longest to wait, in seconds, before giving up.
        poll: How often to re-check the count, in seconds.

    Returns:
        True once the in-flight count is zero; False if ``timeout`` passed first.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout                          # when to stop waiting
    while in_flight_count() > 0:                              # still a write in progress
        if loop.time() >= deadline:
            return False                                      # gave up: the caller fails the switch
        await asyncio.sleep(poll)                             # let the in-flight writes run
    return True                                               # nothing in flight; safe to read SharePoint
