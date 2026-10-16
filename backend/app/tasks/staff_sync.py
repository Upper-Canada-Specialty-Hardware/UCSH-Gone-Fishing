"""Background loop that keeps SharePoint and the Postgres Staff Directory in step (#261).

Once the Staff Directory lives in Postgres, two flows run in the background:

- the write-back drain, which replays app-made changes onto the SharePoint list
  so the Bulletin Board, the Microsoft Form and the flows stay current (#267); and
- the pull, which brings edits made directly in SharePoint into Postgres,
  newer-wins, without echoing them back (#268).

Both only make sense while staff are served from Postgres, so each tick checks
``staff_in_postgres`` first and does nothing while the directory is still on
SharePoint. The drain runs every tick (it is cheap); the pull runs every few
ticks (it reads the list). Errors are logged and never stop the loop, so a
SharePoint outage is ridden out and retried.
"""
import asyncio
import logging

from app.database import async_session
from app.repositories.staff_store import staff_in_postgres
from app.services.staff_pull import staff_pull_step
from app.services.staff_writeback import drain_writebacks

logger = logging.getLogger(__name__)

# How often the loop wakes, and how many wakes between pulls, so the write-back
# is near-immediate while the heavier full-list pull runs only every few minutes.
_TICK_SECONDS = 30
_PULL_EVERY_TICKS = 10


async def _staff_sync_loop() -> None:
    """Drain write-backs every tick and pull SharePoint edits every few ticks."""
    tick = 0
    while True:
        try:
            if await staff_in_postgres():                  # nothing to do while still on SharePoint
                await drain_writebacks()                   # replay app changes to SharePoint
                if tick % _PULL_EVERY_TICKS == 0:          # pull less often; it reads the whole list
                    async with async_session() as session:
                        await staff_pull_step(session, None)
                        await session.commit()
        except Exception:                                  # a bad tick never kills the loop
            logger.exception("Staff sync tick failed")
        tick += 1
        await asyncio.sleep(_TICK_SECONDS)


def start_staff_sync_task() -> asyncio.Task:
    """Start the staff write-back / pull loop as a background task.

    Returns:
        The asyncio Task, so the caller can cancel it on shutdown.
    """
    logger.info("Starting staff sync task (write-back + pull)")
    return asyncio.create_task(_staff_sync_loop())
