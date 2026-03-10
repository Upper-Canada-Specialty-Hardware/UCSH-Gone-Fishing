import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.graph.auth import token_manager
from app.database import async_session
from app.models import WebhookSubscription, ProcessingLog

from sqlalchemy import select, desc

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    # Webhook subscriptions
    async with async_session() as session:
        subs_result = await session.execute(select(WebhookSubscription))
        subscriptions = subs_result.scalars().all()

        # Recent processing log
        log_result = await session.execute(
            select(ProcessingLog).order_by(desc(ProcessingLog.processed_at)).limit(20)
        )
        recent_logs = log_result.scalars().all()

    now = datetime.now(timezone.utc)
    sub_data = []
    for sub in subscriptions:
        exp = sub.expiration
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        days_left = (exp - now).days
        status = "Active" if days_left > 5 else "Expiring Soon" if days_left > 0 else "Expired"
        sub_data.append({
            "id": sub.id,
            "list_id": sub.list_id,
            "expiration": exp.isoformat(),
            "days_left": days_left,
            "status": status,
        })

    return templates.TemplateResponse(
        "status.html",
        {
            "request": request,
            "subscriptions": sub_data,
            "recent_logs": recent_logs,
            "token_valid": token_manager.is_valid,
            "token_expires_in": int(token_manager.seconds_until_expiry),
        },
    )
