import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models import ChangeToken, ProcessingLog
from app.tasks.dispatcher import dispatch_change
from app.tasks.subscription_manager import MONITORED_LISTS

logger = logging.getLogger(__name__)


async def process_notification(notification: dict):
    """Process a single SP webhook notification — query delta, dispatch changes."""
    list_id = notification.get("resource", "").split("/")[-1] if "resource" in notification else None
    client_state = notification.get("clientState")

    # Validate client state against stored subscriptions
    subscription_id = notification.get("subscriptionId")
    async with async_session() as session:
        from app.models import WebhookSubscription
        sub = await session.get(WebhookSubscription, subscription_id)
        if sub and sub.client_state != client_state:
            logger.warning("Client state mismatch for subscription %s", subscription_id)
            return

    if not list_id:
        # Try to extract list_id from the subscription
        if sub:
            list_id = sub.list_id
        else:
            logger.warning("Cannot determine list_id from notification")
            return

    # Get stored change token
    async with async_session() as session:
        token_record = await session.get(ChangeToken, list_id)
        stored_token = token_record.token if token_record else None

    # Query delta
    try:
        delta = await sp_client.get_delta(list_id, stored_token)
    except Exception as e:
        logger.error("Delta query failed for list %s: %s", list_id, e)
        return

    items = delta.get("value", [])
    new_token = None
    # Extract new delta token from @odata.deltaLink
    delta_link = delta.get("@odata.deltaLink", "")
    if "token=" in delta_link:
        new_token = delta_link.split("token=")[-1]

    # Store new change token
    if new_token:
        async with async_session() as session:
            token_record = await session.get(ChangeToken, list_id)
            if token_record:
                token_record.token = new_token
                token_record.updated_at = datetime.now(timezone.utc)
            else:
                session.add(ChangeToken(list_id=list_id, token=new_token, updated_at=datetime.now(timezone.utc)))
            await session.commit()

    # Process each changed item
    for item in items:
        item_id = str(item.get("id", ""))
        if not item_id:
            continue

        # Idempotency check — skip items already successfully processed
        action = "webhook_change"
        async with async_session() as session:
            existing = await session.execute(
                select(ProcessingLog).where(
                    ProcessingLog.list_id == list_id,
                    ProcessingLog.item_id == item_id,
                    ProcessingLog.action == action,
                )
            )
            if existing.scalar_one_or_none():
                continue

        # Dispatch first, then log on success — if the container dies mid-dispatch,
        # the item won't be marked as processed and will be retried on next delta query.
        try:
            await dispatch_change(list_id, item)
        except Exception as e:
            logger.exception("Error dispatching change for list %s, item %s: %s", list_id, item_id, e)
            continue

        async with async_session() as session:
            session.add(ProcessingLog(
                list_id=list_id,
                item_id=item_id,
                action=action,
                processed_at=datetime.now(timezone.utc),
            ))
            await session.commit()


async def catch_up_all_lists():
    """Run delta queries for all monitored lists to process any missed changes.

    Called once on startup to handle items that were missed if a previous
    container died after acknowledging a webhook but before processing completed.
    """
    if not settings.PROCESSING_ENABLED:
        logger.info("Catch-up skipped — processing is disabled")
        return

    total = 0
    for list_id in MONITORED_LISTS:
        try:
            async with async_session() as session:
                token_record = await session.get(ChangeToken, list_id)
                stored_token = token_record.token if token_record else None

            delta = await sp_client.get_delta(list_id, stored_token)
            items = delta.get("value", [])

            # Update delta token
            delta_link = delta.get("@odata.deltaLink", "")
            if "token=" in delta_link:
                new_token = delta_link.split("token=")[-1]
                async with async_session() as session:
                    token_record = await session.get(ChangeToken, list_id)
                    if token_record:
                        token_record.token = new_token
                        token_record.updated_at = datetime.now(timezone.utc)
                    else:
                        session.add(ChangeToken(
                            list_id=list_id, token=new_token,
                            updated_at=datetime.now(timezone.utc),
                        ))
                    await session.commit()

            processed = 0
            for item in items:
                item_id = str(item.get("id", ""))
                if not item_id:
                    continue

                action = "webhook_change"
                async with async_session() as session:
                    existing = await session.execute(
                        select(ProcessingLog).where(
                            ProcessingLog.list_id == list_id,
                            ProcessingLog.item_id == item_id,
                            ProcessingLog.action == action,
                        )
                    )
                    if existing.scalar_one_or_none():
                        continue

                try:
                    await dispatch_change(list_id, item)
                except Exception as e:
                    logger.exception(
                        "Catch-up: error dispatching list %s, item %s: %s",
                        list_id, item_id, e,
                    )
                    continue

                async with async_session() as session:
                    session.add(ProcessingLog(
                        list_id=list_id,
                        item_id=item_id,
                        action=action,
                        processed_at=datetime.now(timezone.utc),
                    ))
                    await session.commit()
                processed += 1

            total += processed
            if processed:
                logger.info("Catch-up: processed %d items for list %s", processed, list_id)
            else:
                logger.info("Catch-up: no unprocessed items for list %s", list_id)

        except Exception as e:
            logger.exception("Catch-up: failed for list %s: %s", list_id, e)

    logger.info("Catch-up complete — processed %d total items", total)
