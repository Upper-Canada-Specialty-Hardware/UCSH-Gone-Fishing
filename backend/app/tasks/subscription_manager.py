import asyncio
import logging

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
    # Rehearsal mode registers no Graph webhooks, so a rehearsal never makes the
    # real SharePoint lists call this deployment (#339).
    from app.services.rehearsal import rehearsal_enabled
    if rehearsal_enabled():
        logger.info("Rehearsal: skipping Graph webhook subscription registration")
        return
    # After the switch to Postgres the SharePoint lists are never used again, so
    # no webhook is registered or renewed (the switch deleted the old ones).
    from app.services.sharepoint_cutoff import sharepoint_cut_off
    if await sharepoint_cut_off():
        logger.info("Switched to Postgres: not registering SharePoint webhooks")
        return
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


async def delete_all_subscriptions() -> int:
    """Delete every stored webhook subscription from Graph and from the table.

    Run by the storage switch just before it flips the settings, so SharePoint
    stops calling this app once Postgres is the source of truth. A subscription
    Graph no longer knows is still removed from the table. In rehearsal mode
    nothing is sent to Graph (no subscriptions were registered there anyway).

    Returns:
        How many stored subscriptions were removed.
    """
    from app.services.rehearsal import sharepoint_writes_allowed
    removed = 0
    async with async_session() as session:
        rows = (await session.execute(select(WebhookSubscription))).scalars().all()
        for row in rows:
            if sharepoint_writes_allowed():                    # never touch live Graph in rehearsal
                try:
                    await delete_subscription(row.id, row.list_id)
                except Exception:
                    # Already gone, or Graph is down: it lapses on its own, and the
                    # webhook route ignores notifications once switched.
                    logger.warning("Could not delete subscription %s from Graph", row.id)
            await session.delete(row)                          # forget it either way
            removed += 1
        await session.commit()
    logger.info("Switch: removed %d SharePoint webhook subscription(s)", removed)
    return removed


async def _renewal_loop():
    """Background task: check and renew subscriptions daily, until the switch."""
    from app.services.sharepoint_cutoff import sharepoint_cut_off
    while True:
        await asyncio.sleep(RENEWAL_INTERVAL)
        if await sharepoint_cut_off():                         # switched: never renew again
            logger.info("Switched to Postgres: webhook renewal stopped")
            return
        logger.info("Running subscription renewal check...")
        try:
            await register_all_subscriptions()
        except Exception as e:
            logger.exception("Subscription renewal failed: %s", e)


def start_subscription_renewal_task() -> asyncio.Task:
    return asyncio.create_task(_renewal_loop())
