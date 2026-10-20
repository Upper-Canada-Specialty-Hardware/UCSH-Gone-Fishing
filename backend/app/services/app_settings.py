"""Settings an admin changes from the dashboard, stored in Postgres.

Each setting is one row of app_settings, by name. A setting with no row has
its default, so a fresh database behaves like a closed, untouched one.

The first one is CARRYOVER_PAYOUT_WINDOW (#176): the dates between which staff
may send carry-over and payout requests. HR only takes them at one time of
year, so there are no dates by default, which means closed.
"""

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.database import async_session
from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

CARRYOVER_PAYOUT_WINDOW = "carryover_payout_window"   # {"start", "end"} ISO dates, or None
REQUESTS_STORAGE = "requests_storage"                 # "sharepoint" (default) or "postgres"
STAFF_STORAGE = "staff_storage"                       # "sharepoint" (default) or "postgres"
HOLIDAYS_STORAGE = "holidays_storage"                 # "sharepoint" (default) or "postgres"
SWITCH_PAUSE = "switch_pause"                         # {"until": ISO} while the storage switch runs, or None
TORONTO = ZoneInfo("America/Toronto")                # the company's calendar day

# The two backends the request lists can live in; the switch reads this.
SHAREPOINT = "sharepoint"
POSTGRES = "postgres"

# Each setting's value while it has no row.
DEFAULTS: dict[str, Any] = {
    CARRYOVER_PAYOUT_WINDOW: None,                  # no dates: closed until an admin sets them
    REQUESTS_STORAGE: SHAREPOINT,                   # requests stay in SharePoint until the switch is flipped
    STAFF_STORAGE: SHAREPOINT,                      # the Staff Directory stays in SharePoint until the switch is flipped
    HOLIDAYS_STORAGE: SHAREPOINT,                   # Company Holidays stay in SharePoint until the switch is flipped
    SWITCH_PAUSE: None,                             # no pause until the switch sets one; None means the app is open
}


async def get_setting(key: str) -> Any:
    """One setting's value, or its default when it was never set.

    Args:
        key: A setting name from DEFAULTS.

    Returns:
        The stored value, or DEFAULTS[key].
    """
    async with async_session() as session:
        row = await session.get(AppSetting, key)
    return row.value if row is not None else DEFAULTS[key]


async def set_setting(key: str, value: Any) -> Any:
    """Store one setting's value, creating its row the first time.

    Args:
        key: A setting name from DEFAULTS.
        value: The new value; must be JSON-serialisable.

    Returns:
        The value stored.

    Raises:
        KeyError: For a name not in DEFAULTS, so a typo cannot create a setting.
    """
    if key not in DEFAULTS:
        raise KeyError(key)
    async with async_session() as session:
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))       # first change from the default
        else:
            row.value = value
        await session.commit()
    logger.info("Setting %s changed to %r", key, value)
    return value


async def requests_storage() -> str:
    """Where the request lists are read and written now, from the database.

    Read on every call so an admin flipping the switch takes effect without a
    redeploy. The request store validates the value; this only returns it.

    Returns:
        The stored backend name, or "sharepoint" when it was never set.
    """
    return await get_setting(REQUESTS_STORAGE)


async def staff_storage() -> str:
    """Where the Staff Directory is read and written now, from the database.

    Read on every call, like ``requests_storage``, so an admin flipping the
    switch takes effect without a redeploy. The staff store validates the value;
    this only returns it.

    Returns:
        The stored backend name, or "sharepoint" when it was never set.
    """
    return await get_setting(STAFF_STORAGE)


async def holidays_storage() -> str:
    """Where Company Holidays are read and written now, from the database.

    Read on every call, like ``requests_storage`` and ``staff_storage``, so an
    admin flipping the switch takes effect without a redeploy. The holiday
    factory validates the value; this only returns it.

    Returns:
        The stored backend name, or "sharepoint" when it was never set.
    """
    return await get_setting(HOLIDAYS_STORAGE)


def toronto_today() -> date:
    """Today's date in Toronto, the calendar the window is set in.

    Returns:
        The Toronto calendar date right now.
    """
    return datetime.now(TORONTO).date()


def parse_window(value: Any) -> tuple[date, date] | None:
    """Read a stored window back into dates.

    Args:
        value: The stored setting: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} or None.

    Returns:
        (start, end), or None when no usable dates are stored.
    """
    if not isinstance(value, dict):
        return None                                            # never set, or cleared
    try:
        return date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
    except (KeyError, TypeError, ValueError):
        logger.warning("Ignoring an unreadable carry-over window: %r", value)
        return None                                            # unreadable counts as closed


def window_is_open(window: tuple[date, date] | None, today: date) -> bool:
    """Whether a day falls inside the window, both ends included.

    Args:
        window: (start, end) or None.
        today: The day to test.

    Returns:
        False with no window; otherwise start <= today <= end.
    """
    return window is not None and window[0] <= today <= window[1]


async def carryover_payout_window() -> tuple[date, date] | None:
    """The dates carry-over and payout requests are accepted between.

    Returns:
        (start, end), or None when an admin has not set them.
    """
    return parse_window(await get_setting(CARRYOVER_PAYOUT_WINDOW))


async def set_carryover_payout_window(start: date | None, end: date | None) -> tuple[date, date] | None:
    """Store the window, or clear it with no dates.

    Args:
        start: First day requests are accepted, or None to clear.
        end: Last day requests are accepted, or None to clear.

    Returns:
        The stored (start, end), or None once cleared.

    Raises:
        ValueError: Only one date given, or the end before the start.
    """
    if start is None and end is None:
        await set_setting(CARRYOVER_PAYOUT_WINDOW, None)       # cleared: closed
        return None
    if start is None or end is None:
        raise ValueError("Set both a start and an end date, or clear both.")
    if end < start:
        raise ValueError("The end date cannot be before the start date.")
    await set_setting(CARRYOVER_PAYOUT_WINDOW, {"start": start.isoformat(), "end": end.isoformat()})
    return start, end


async def carryover_payout_open(today: date | None = None) -> bool:
    """Whether staff may send carry-over and payout requests on a given day.

    Args:
        today: The day to test; defaults to today in Toronto.

    Returns:
        True only inside the window an admin set.
    """
    return window_is_open(await carryover_payout_window(), today or toronto_today())
