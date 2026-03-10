import asyncio
import logging

from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from app.config import settings
from app.tasks.change_processor import process_notification

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sharepoint", response_class=PlainTextResponse)
async def sharepoint_webhook(
    request: Request,
    validationToken: str | None = Query(None),
):
    # Subscription validation handshake
    if validationToken:
        logger.info("SP webhook validation — echoing token")
        return PlainTextResponse(content=validationToken, status_code=200)

    # Change notification — respond 202 immediately, process async
    body = await request.json()
    notifications = body.get("value", [])
    logger.info("Received %d SP webhook notification(s)", len(notifications))

    if settings.PROCESSING_ENABLED:
        for notification in notifications:
            asyncio.create_task(process_notification(notification))
    else:
        logger.info("Processing disabled — acknowledging %d notification(s) without processing", len(notifications))

    return JSONResponse(content={}, status_code=202)
