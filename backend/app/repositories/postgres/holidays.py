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
from datetime import date, datetime

from sqlalchemy import select

from app.database import async_session
from app.models.holiday import Holiday
from app.repositories.base import HolidayRepository

logger = logging.getLogger(__name__)

# SharePoint column name -> Holiday model attribute; the only writable fields.
_FIELD_TO_COLUMN = {
    "Title": "title",
    "Date": "date",
    "Province": "province",
}


def _parse_date(value):
    """Accept what callers send for Date: an ISO string, a date, or empty."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _translate(fields: dict) -> dict:
    """Translate a SharePoint-shaped field payload into model column values.

    Args:
        fields: SharePoint column names -> values (Title, Date, Province).

    Returns:
        Model attribute -> value dict.

    Raises:
        KeyError: On a field name outside _FIELD_TO_COLUMN — a dropped write
            would silently lose data, so it fails loudly (same rule as the
            employee repository).
    """
    values = {}
    unknown = []
    for sp_name, value in fields.items():
        column = _FIELD_TO_COLUMN.get(sp_name)
        if column is None:
            unknown.append(sp_name)  # collect, then raise once
            continue
        values[column] = _parse_date(value) if sp_name == "Date" else value
    if unknown:
        raise KeyError(
            f"Cannot write unmapped holiday field(s) {sorted(unknown)} to Postgres. "
            f"Add them to _FIELD_TO_COLUMN rather than letting the write be dropped."
        )
    return values


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

    async def create(self, fields: dict) -> dict:
        """Insert a holiday row from a SharePoint-shaped payload.

        Args:
            fields: SharePoint column names -> values (Title, Date, Province).

        Returns:
            The created holiday as {"id","fields"}.

        Raises:
            KeyError: On an unmapped field name (see _translate).
        """
        values = _translate(fields)  # validate + map before touching the db
        async with async_session() as session:
            # A Postgres-native holiday has no SharePoint item backing it; mint
            # the next numeric id above every existing one so it cannot collide
            # with a backfilled SharePoint id (same scheme as the employee repo).
            existing = (await session.execute(select(Holiday.sp_item_id))).scalars()
            numeric = [int(s) for s in existing if s is not None and str(s).isdigit()]
            new_id = str(max(numeric, default=0) + 1)  # strictly above the current max
            session.add(Holiday(sp_item_id=new_id, **values))
            await session.commit()
        return await self.get_by_id(new_id)  # return the canonical shape

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Patch a holiday row from a SharePoint-shaped payload.

        Args:
            item_id: The holiday's sp_item_id.
            fields: SharePoint column names -> new values.

        Returns:
            The updated holiday as {"id","fields"}.

        Raises:
            KeyError: If the holiday does not exist, or on an unmapped field.
        """
        values = _translate(fields)  # validate + map before touching the db
        async with async_session() as session:
            row = (
                await session.execute(select(Holiday).where(Holiday.sp_item_id == str(item_id)))
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"No holiday with id {item_id}")
            for column, value in values.items():
                setattr(row, column, value)  # apply each translated column
            await session.commit()
        return await self.get_by_id(item_id)

    async def delete(self, item_id: str | int) -> None:
        """Delete a holiday row permanently.

        Args:
            item_id: The holiday's sp_item_id.

        Raises:
            KeyError: If the holiday does not exist — a delete that silently
                does nothing would hide a stale admin view.
        """
        async with async_session() as session:
            row = (
                await session.execute(select(Holiday).where(Holiday.sp_item_id == str(item_id)))
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"No holiday with id {item_id}")
            await session.delete(row)
            await session.commit()
