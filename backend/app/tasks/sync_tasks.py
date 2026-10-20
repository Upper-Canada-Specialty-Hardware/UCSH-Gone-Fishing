"""The background loop that drives the shadow sync (#334).

While requests still live in SharePoint, the shadow sync runs every five minutes
and keeps the Postgres copy of staff, requests and holidays current. Once the
switch has run it does nothing (``run_shadow_sync`` returns at once), so the loop
needs no restart. It swallows its own errors so a bad run never stops it; the
next tick tries again.

There is no SharePoint loop after the switch: Postgres is the only source, and
nothing reads or writes the SharePoint lists again.
"""
import asyncio
import logging

from app.services.shadow_sync import run_shadow_sync

logger = logging.getLogger(__name__)

SHADOW_INTERVAL = 300                   # run the shadow sync every 5 minutes


async def _shadow_loop() -> None:
    """Run the shadow sync on its interval, until the app shuts down."""
    while True:
        await asyncio.sleep(SHADOW_INTERVAL)
        try:
            await run_shadow_sync()                            # no-ops once requests are in Postgres
        except Exception:
            logger.exception("Shadow sync loop iteration failed")


def start_shadow_sync_task() -> asyncio.Task:
    """Start the shadow-sync loop.

    Returns:
        The created asyncio task, to cancel on shutdown.
    """
    return asyncio.create_task(_shadow_loop())
