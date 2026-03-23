import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.webhooks import create_subscription, delete_subscription
from app.models import WebhookSubscription

logger = logging.getLogger(__name__)

MONITORED_LISTS = [
    settings.SP_LIST_LEAVE_REQUESTS,
    settings.SP_LIST_OVERTIME_REQUESTS,
    settings.SP_LIST_CARRYOVER_PAYOUT,
    settings.SP_LIST_COMPANY_HOLIDAYS,
]

RENEWAL_INTERVAL = 86400  # 24 hours
RENEWAL_THRESHOLD_DAYS = 5


async def register_all_subscriptions():
    """Register or renew webhook subscriptions for all monitored lists."""
    for list_id in MONITORED_LISTS:
        try:
            await _ensure_subscription(list_id)
        except Exception as e:
            logger.error("Failed to register subscription for list %s: %s", list_id, e)


async def _ensure_subscription(list_id: str):
    """Delete any existing subscription and create a fresh one.

    Always recreates on startup to guarantee the client_state in our DB
    matches what Graph API sends in webhook notifications. Previous
    approach of reusing "still valid" subscriptions caused client_state
    mismatches when the DB record was stale.
    """
    # Clean up any existing subscriptions for this list
    async with async_session() as session:
        result = await session.execute(
            select(WebhookSubscription).where(WebhookSubscription.list_id == list_id)
        )
        for existing in result.scalars():
            try:
                await delete_subscription(existing.id, list_id)
            except Exception:
                logger.debug("Could not delete old subscription %s from Graph (may already be gone)", existing.id)
            await session.delete(existing)
        await session.commit()

    # Create fresh subscription
    sub_data = await create_subscription(list_id)
    async with async_session() as session:
        session.add(WebhookSubscription(
            id=sub_data["id"],
            list_id=sub_data["list_id"],
            expiration=sub_data["expiration"],
            client_state=sub_data["client_state"],
        ))
        await session.commit()
    logger.info("Registered new subscription for list %s", list_id)


async def _renewal_loop():
    """Background task: check and renew subscriptions daily."""
    while True:
        await asyncio.sleep(RENEWAL_INTERVAL)
        logger.info("Running subscription renewal check...")
        try:
            await register_all_subscriptions()
        except Exception as e:
            logger.exception("Subscription renewal failed: %s", e)


def start_subscription_renewal_task() -> asyncio.Task:
    return asyncio.create_task(_renewal_loop())
