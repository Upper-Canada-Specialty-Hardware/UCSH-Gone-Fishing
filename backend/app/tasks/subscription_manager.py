import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.webhooks import create_subscription, renew_subscription, delete_subscription
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
    """Check if active subscription exists, create or renew as needed."""
    async with async_session() as session:
        result = await session.execute(
            select(WebhookSubscription).where(WebhookSubscription.list_id == list_id)
        )
        existing = result.scalar_one_or_none()

    now = datetime.utcnow()

    if existing:
        exp = existing.expiration
        if exp - now > timedelta(days=RENEWAL_THRESHOLD_DAYS):
            logger.info("Subscription for list %s is still valid (expires %s)", list_id, exp)
            return
        # Renew
        try:
            new_exp = await renew_subscription(existing.id, list_id)
            async with async_session() as session:
                sub = await session.get(WebhookSubscription, existing.id)
                if sub:
                    sub.expiration = new_exp
                    await session.commit()
            return
        except Exception as e:
            logger.warning("Failed to renew subscription %s, will delete and recreate: %s", existing.id, e)

        # Clean up old subscription before creating new
        await _delete_subscription_record(existing.id, list_id)

    # Create new subscription
    sub_data = await create_subscription(list_id)
    async with async_session() as session:
        # Remove any stale DB records for this list before inserting
        stale = await session.execute(
            select(WebhookSubscription).where(WebhookSubscription.list_id == list_id)
        )
        for old in stale.scalars():
            await session.delete(old)
        session.add(WebhookSubscription(
            id=sub_data["id"],
            list_id=sub_data["list_id"],
            expiration=sub_data["expiration"],
            client_state=sub_data["client_state"],
        ))
        await session.commit()
    logger.info("Registered new subscription for list %s", list_id)


async def _delete_subscription_record(subscription_id: str, list_id: str):
    """Delete subscription from Graph API and DB."""
    try:
        await delete_subscription(subscription_id, list_id)
    except Exception:
        logger.debug("Could not delete old subscription %s from Graph (may already be gone)", subscription_id)
    async with async_session() as session:
        sub = await session.get(WebhookSubscription, subscription_id)
        if sub:
            await session.delete(sub)
            await session.commit()


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
