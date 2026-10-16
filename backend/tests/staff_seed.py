"""Test helpers: put a faked Staff Directory into the Postgres staff tables.

Tests that pin staff to Postgres (``STORAGE_EMPLOYEES=postgres``) read people
from the ``staff`` table, not from a faked SharePoint list. These helpers fill
that table the way production does before the switch: by running the shadow
sync's staff import against whatever ``sp_client.get_list_items`` returns (the
test's fake). Staff keep their SharePoint item ids, so ids in a test's fake
list match the ids the app answers with.
"""
from sqlalchemy import delete

from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.staff import Staff, StaffSupervisor, StaffWriteback
from app.models.staff_change import StaffChange
from app.services.staff_import import staff_sync_step


async def wipe_staff() -> None:
    """Empty every staff table (children first, so foreign keys hold)."""
    async with async_session() as session:
        for model in (StaffWriteback, StaffSupervisor, StaffChange, LegacyFinding, Staff):
            await session.execute(delete(model))
        await session.commit()


async def import_staff() -> dict:
    """Import the faked Staff Directory into Postgres, as the shadow sync would.

    Returns:
        The staff import's counts, so a test can check what landed.
    """
    async with async_session() as session:
        counts = await staff_sync_step(session, None)          # reads the faked sp_client list
        await session.commit()
    return counts
