import logging

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
