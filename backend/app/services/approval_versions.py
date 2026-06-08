import logging
from datetime import datetime

from app.database import async_session
from app.models import RequestApprovalState

logger = logging.getLogger(__name__)

MATERIAL_FIELDS_LEAVE = ("Days", "LeaveType", "StartDate", "EndDate")
MATERIAL_FIELDS_OVERTIME = ("Hours", "StartDate", "Title")
MATERIAL_FIELDS_CARRYOVER_PAYOUT = ("TypeofRequest", "Days")


def extract_snapshot(fields: dict, material_keys: tuple[str, ...]) -> dict:
    return {k: fields.get(k) for k in material_keys}


async def bump_and_snapshot(
    list_id: str,
    item_id: str | int,
    current_fields: dict,
    material_keys: tuple[str, ...],
) -> int:
    """Record the current material-field snapshot for an outgoing approval email.

    Returns the version embedded in the link being sent. Bumps the version only
    when material fields actually differ from the prior snapshot — re-sends with
    no value change keep the version stable so existing links keep working.
    """
    new_snapshot = extract_snapshot(current_fields, material_keys)
    item_id_str = str(item_id)
    now = datetime.utcnow()

    async with async_session() as session:
        row = await session.get(RequestApprovalState, (list_id, item_id_str))
        if row is None:
            row = RequestApprovalState(
                list_id=list_id,
                item_id=item_id_str,
                current_version=1,
                current_snapshot=new_snapshot,
                previous_snapshot=None,
                last_emailed_at=now,
            )
            session.add(row)
            await session.commit()
            return 1

        if row.current_snapshot == new_snapshot:
            row.last_emailed_at = now
            await session.commit()
            return row.current_version

        row.previous_snapshot = row.current_snapshot
        row.current_snapshot = new_snapshot
        row.current_version += 1
        row.last_emailed_at = now
        await session.commit()
        return row.current_version


async def get_current_version(list_id: str, item_id: str | int) -> int:
    """Return the latest version for this item, or 1 if no row exists yet."""
    async with async_session() as session:
        row = await session.get(RequestApprovalState, (list_id, str(item_id)))
        return row.current_version if row else 1


async def get_previous_snapshot(list_id: str, item_id: str | int) -> dict | None:
    """Return the previous-version snapshot (one before current), or None."""
    async with async_session() as session:
        row = await session.get(RequestApprovalState, (list_id, str(item_id)))
        return row.previous_snapshot if row else None
