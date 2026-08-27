"""Company Holidays served from the Postgres ``holidays`` table.

First domain across the seam. ``services/holidays.py`` already reads through
the repository rather than sp_client, so switching STORAGE_HOLIDAYS to
"postgres" swaps the source of these rows without touching the business-day
calculator, the half-Friday season logic, or anything that consumes them.

The rows must be present before the flag is flipped -- run the SharePoint ->
Postgres backfill for holidays first. An empty table is not an error to the
calculator, it simply means "no stat holidays", which silently changes how
business days are counted, so ``get_all`` logs a warning if it ever reads zero
rows.
"""
import logging

from sqlalchemy import select

from app.database import async_session
from app.models.holiday import Holiday
from app.repositories.base import HolidayRepository

logger = logging.getLogger(__name__)


def _to_sp_shape(row: Holiday) -> dict:
    """Rebuild a SharePoint list item from a Holiday row.

    ``id`` is the SharePoint item id (``sp_item_id``) rather than the Postgres
    primary key, so an id captured while SharePoint was the source of record
    still resolves after the cutover.

    ``Date`` is an ISO date string because that is what Graph returns and what
    ``services.holidays._parse_date`` is written against -- it accepts a date
    object too, but emitting the string keeps the two backends byte-identical
    to any caller that slices or compares the raw value.
    """
    return {
        "id": row.sp_item_id,
        "fields": {
            "Title": row.title,
            "Date": row.date.isoformat() if row.date else None,
            "Province": row.province,
        },
    }


class PostgresHolidayRepository(HolidayRepository):
    """Company Holidays backed by Postgres.

    Mirrors SharePointHolidayRepository: it returns every row and lets the
    service filter by province client-side, so the two are interchangeable.
    """

    async def get_all(self) -> list[dict]:
        async with async_session() as session:
            # Ordered by primary key for a deterministic read; nothing downstream
            # depends on ordering, it just makes reads reproducible.
            result = await session.execute(select(Holiday).order_by(Holiday.id))
            rows = list(result.scalars())

        if not rows:
            logger.warning(
                "STORAGE_HOLIDAYS is 'postgres' but the holidays table is empty — "
                "business-day calculations will exclude no stat holidays. "
                "Has the holidays backfill been run?"
            )
        return [_to_sp_shape(row) for row in rows]

    async def get_by_id(self, item_id: str | int) -> dict | None:
        async with async_session() as session:
            result = await session.execute(
                select(Holiday).where(Holiday.sp_item_id == str(item_id))
            )
            row = result.scalar_one_or_none()
        return _to_sp_shape(row) if row else None
