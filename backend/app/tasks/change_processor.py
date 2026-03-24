import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
                token_record.updated_at = datetime.utcnow()
            else:
                session.add(ChangeToken(list_id=list_id, token=new_token, updated_at=datetime.utcnow()))
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
                processed_at=datetime.utcnow(),
            ))
            await session.commit()


async def catch_up_all_lists():
    """Query SP lists for items that still need processing and dispatch them.

    Called once on startup. Unlike the old delta-based approach, this directly
    queries each list for items whose SP state indicates they haven't been
    fully auto-processed (no manager assigned). This handles items missed by
    stale delta tokens, admin-created SP items, and partial processing failures.

    Phase 1: Direct-query each list, dispatch unprocessed items.
    Phase 2: Refresh delta tokens so future webhooks work correctly.
    """
    if not settings.PROCESSING_ENABLED:
        logger.info("Catch-up skipped — processing is disabled")
        return

    # --- Phase 1: Direct query and dispatch ---
    total = 0

    # Leave Requests: Pending with no manager assigned
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        need_processing = [
            i for i in items
            if i.get("fields", {}).get("Status") == "Pending"
            and not i.get("fields", {}).get("Managertxt")
        ]
        processed = await _dispatch_and_log(
            settings.SP_LIST_LEAVE_REQUESTS, need_processing, "leave request",
        )
        total += processed
        logger.info(
            "Catch-up: leave requests — %d pending, %d need processing, %d dispatched",
            len(items), len(need_processing), processed,
        )
    except Exception:
        logger.exception("Catch-up: failed to process leave requests")

    # Overtime Requests: Pending with no manager assigned
    # Note: Status is not indexed on this list, so fetch all and filter client-side
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        need_processing = [
            i for i in items
            if i.get("fields", {}).get("Status") == "Pending"
            and not i.get("fields", {}).get("ManagerLookupId")
        ]
        processed = await _dispatch_and_log(
            settings.SP_LIST_OVERTIME_REQUESTS, need_processing, "overtime request",
        )
        total += processed
        logger.info(
            "Catch-up: overtime requests — %d total, %d need processing, %d dispatched",
            len(items), len(need_processing), processed,
        )
    except Exception:
        logger.exception("Catch-up: failed to process overtime requests")

    # Carryover/Payout: No manager, SystemState absent or "Not Processed"
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
        need_processing = []
        for i in items:
            f = i.get("fields", {})
            if f.get("Managertxt"):
                continue
            system_state = f.get("SystemState")
            if system_state and system_state != "Not Processed":
                continue
            need_processing.append(i)
        processed = await _dispatch_and_log(
            settings.SP_LIST_CARRYOVER_PAYOUT, need_processing, "carryover/payout",
        )
        total += processed
        logger.info(
            "Catch-up: carryover/payout — %d total, %d need processing, %d dispatched",
            len(items), len(need_processing), processed,
        )
    except Exception:
        logger.exception("Catch-up: failed to process carryover/payout requests")

    logger.info("Catch-up Phase 1 complete — %d items dispatched", total)

    # --- Phase 2: Refresh delta tokens for future webhook use ---
    for list_id in MONITORED_LISTS:
        try:
            async with async_session() as session:
                token_record = await session.get(ChangeToken, list_id)
                stored_token = token_record.token if token_record else None

            try:
                delta = await sp_client.get_delta(list_id, stored_token)
            except Exception:
                logger.info(
                    "Catch-up: stale delta token for list %s, falling back to tokenless",
                    list_id,
                )
                delta = await sp_client.get_delta(list_id, None)

            delta_link = delta.get("@odata.deltaLink", "")
            if "token=" in delta_link:
                new_token = delta_link.split("token=")[-1]
                async with async_session() as session:
                    token_record = await session.get(ChangeToken, list_id)
                    if token_record:
                        token_record.token = new_token
                        token_record.updated_at = datetime.utcnow()
                    else:
                        session.add(ChangeToken(
                            list_id=list_id, token=new_token,
                            updated_at=datetime.utcnow(),
                        ))
                    await session.commit()
                logger.info("Catch-up: refreshed delta token for list %s", list_id)
        except Exception:
            logger.exception("Catch-up: failed to refresh delta token for list %s", list_id)

    logger.info("Catch-up complete — %d items dispatched, delta tokens refreshed", total)


async def _dispatch_and_log(list_id: str, items: list[dict], label: str) -> int:
    """Dispatch items via the dispatcher and write processing_log on success."""
    processed = 0
    for item in items:
        item_id = str(item.get("id", ""))
        if not item_id:
            continue
        try:
            await dispatch_change(list_id, item)
        except Exception:
            logger.exception("Catch-up: error dispatching %s #%s", label, item_id)
            continue
        try:
            async with async_session() as session:
                session.add(ProcessingLog(
                    list_id=list_id,
                    item_id=item_id,
                    action="webhook_change",
                    processed_at=datetime.utcnow(),
                ))
                await session.commit()
        except IntegrityError:
            pass  # Already logged from a previous run
        processed += 1
    return processed
