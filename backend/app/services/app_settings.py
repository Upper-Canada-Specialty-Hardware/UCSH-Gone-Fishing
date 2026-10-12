"""Settings an admin changes from the dashboard, stored in Postgres.

Each setting is one row of app_settings, by name. A setting with no row has
its default, so a fresh database behaves like a closed, untouched one.

The first one is CARRYOVER_PAYOUT_OPEN (#203): whether staff may send
carry-over and payout requests. HR only takes them at one time of year, so it
is off by default and an admin turns it on for that period.
"""

import logging
from typing import Any

from app.database import async_session
from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

CARRYOVER_PAYOUT_OPEN = "carryover_payout_open"   # bool: carry-over and payout requests accepted

# Each setting's value while it has no row.
DEFAULTS: dict[str, Any] = {
    CARRYOVER_PAYOUT_OPEN: False,                   # closed until an admin opens them
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


async def carryover_payout_open() -> bool:
    """Whether staff may send carry-over and payout requests right now.

    Returns:
        True only after an admin opened them.
    """
    return bool(await get_setting(CARRYOVER_PAYOUT_OPEN))
