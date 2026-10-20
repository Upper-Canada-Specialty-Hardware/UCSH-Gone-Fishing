"""Copy Company Holidays from SharePoint into the Postgres ``holidays`` table (#335).

This is the holidays leg of the shadow sync and the flip's final sync. Like the
staff and request steps it reads SharePoint and writes Postgres, never the other
way, so SharePoint stays the source of record until the switch flips. Each run
upserts every Company Holidays list item by its SharePoint item id (idempotent)
and deletes any holiday row whose SharePoint item is gone, so the table mirrors
SharePoint exactly.

The step writes through the session the sync runner hands it, not through
``PostgresHolidayRepository`` (which opens and commits its own session). Nesting a
second committing session inside the shared sync session deadlocks SQLite, the
same lock the final sync hit before it was split one-session-per-step, so the
copy is done with plain ORM calls on the passed session.
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.graph.sharepoint import sp_client
from app.models.holiday import Holiday
from app.models.sync_run import SyncRun
from app.repositories.postgres.holidays import _parse_date  # Graph ISO date -> date, shared

logger = logging.getLogger(__name__)


async def holiday_sync_step(session: AsyncSession, sync_run: SyncRun) -> dict:
    """Mirror SharePoint Company Holidays into the ``holidays`` table.

    Reads every Company Holidays list item from SharePoint, upserts each into the
    ``holidays`` table keyed on ``sp_item_id``, and deletes any holiday row whose
    SharePoint item no longer exists. A truncated SharePoint read raises from
    ``sp_client`` and propagates, so the flip's final sync fails rather than
    copying a partial list.

    Args:
        session: The sync run's open session; the caller commits it.
        sync_run: The SyncRun row this step belongs to; kept for the uniform step
            signature, not otherwise used here.

    Returns:
        ``{"seen", "imported", "updated", "deleted"}`` counts for SyncRun.counts.
    """
    items = await sp_client.get_list_items(settings.SP_LIST_COMPANY_HOLIDAYS)  # raw SP list items
    seen_ids: set[str] = set()                               # every sp id SharePoint still has
    imported = 0                                             # rows newly inserted this run
    updated = 0                                              # rows overwritten this run
    for item in items:
        sp_id = str(item.get("id"))                          # the SharePoint item id, the upsert key
        seen_ids.add(sp_id)
        fields = item.get("fields", {})                      # the item's SharePoint columns
        row = (
            await session.execute(select(Holiday).where(Holiday.sp_item_id == sp_id))
        ).scalar_one_or_none()                               # the existing row, or None
        if row is None:
            row = Holiday(sp_item_id=sp_id)                  # new row, keep the SharePoint id
            session.add(row)
            imported += 1
        else:
            updated += 1
        row.title = fields.get("Title")                      # copy the three mapped columns
        row.date = _parse_date(fields.get("Date"))           # parse Graph's ISO date to a date
        row.province = fields.get("Province")
    deleted = 0                                              # rows SharePoint dropped since last run
    existing = (await session.execute(select(Holiday))).scalars().all()
    for row in existing:
        if row.sp_item_id not in seen_ids:                   # SharePoint no longer has it
            await session.delete(row)
            deleted += 1
    await session.flush()                                    # surface any db error inside this step
    logger.info(
        "Holiday sync: %d read, %d imported, %d updated, %d deleted",
        len(items), imported, updated, deleted,
    )
    return {"seen": len(items), "imported": imported, "updated": updated, "deleted": deleted}
