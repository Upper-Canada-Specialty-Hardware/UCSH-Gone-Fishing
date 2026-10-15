import logging

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models import ProcessingLog

logger = logging.getLogger(__name__)


async def claim_action(list_id: str, item_id: str | int, action: str) -> bool:
    """Atomically claim (list_id, item_id, action) in processing_log.

    Returns True if this call is the first to claim the key — caller should proceed.
    Returns False if another call already claimed it — caller should stop.

    Works across concurrent requests and backend instances via the table's
    UniqueConstraint("list_id", "item_id", "action"). Postgres raises
    IntegrityError on the duplicate insert and we convert that into False.
    """
    async with async_session() as session:
        session.add(ProcessingLog(
            list_id=list_id,
            item_id=str(item_id),
            action=action,
        ))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            logger.info("Idempotency: duplicate claim blocked for %s/%s/%s", list_id, item_id, action)
            return False


def approval_email_action(version: int) -> str:
    """The processing_log action that marks one approval email version as sent.

    Args:
        version: The approval version the email's links carry.

    Returns:
        The action name, e.g. "approval_email_v1".
    """
    return f"approval_email_v{version}"                     # one key per version: a new version sends again


async def release_claim(list_id: str, item_id: str | int, action: str) -> None:
    """Undo a claim, so the work it guarded can be tried again.

    Used when the guarded work failed after the claim was taken: without this,
    a send that raised would still count as sent and never be retried.

    Args:
        list_id: The SharePoint list id the claim was taken for.
        item_id: The list item id.
        action: The claimed action name.
    """
    async with async_session() as session:
        await session.execute(
            delete(ProcessingLog).where(                       # the one row claim_action added
                ProcessingLog.list_id == list_id,
                ProcessingLog.item_id == str(item_id),
                ProcessingLog.action == action,
            )
        )
        await session.commit()
