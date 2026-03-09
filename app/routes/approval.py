import logging

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.approval_links import validate_approval_token
from app.services.leave_requests import approve_leave_request, reject_leave_request
from app.services.overtime_requests import approve_overtime_request, reject_overtime_request
from app.services.carryover_payout import approve_carryover_payout, reject_carryover_payout

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

HANDLERS = {
    ("leave", "approve"): approve_leave_request,
    ("leave", "reject"): reject_leave_request,
    ("overtime", "approve"): approve_overtime_request,
    ("overtime", "reject"): reject_overtime_request,
    ("carryover-payout", "approve"): approve_carryover_payout,
    ("carryover-payout", "reject"): reject_carryover_payout,
}


@router.get("/{request_type}/{action}/{request_id}", response_class=HTMLResponse)
async def handle_approval(
    request: Request,
    request_type: str,
    action: str,
    request_id: str,
    token: str = Query(...),
    mgr: str = Query(...),
    exp: str = Query(...),
):
    # Gate behind processing toggle
    if not settings.PROCESSING_ENABLED:
        return templates.TemplateResponse(
            "approval_error.html",
            {"request": request, "error": "System is currently in reporting-only mode. Approvals are disabled.", "request_id": request_id},
        )

    # Validate HMAC token
    valid, error_msg = validate_approval_token(
        request_type, request_id, action, mgr, token, exp
    )
    if not valid:
        return templates.TemplateResponse(
            "approval_error.html",
            {"request": request, "error": error_msg, "request_id": request_id},
        )

    # Look up handler
    handler = HANDLERS.get((request_type, action))
    if not handler:
        return templates.TemplateResponse(
            "approval_error.html",
            {"request": request, "error": "Invalid request type or action", "request_id": request_id},
        )

    try:
        result = await handler(request_id, mgr)
    except Exception as e:
        logger.exception("Error processing approval for %s #%s", request_type, request_id)
        return templates.TemplateResponse(
            "approval_error.html",
            {"request": request, "error": str(e), "request_id": request_id},
        )

    if "error" in result:
        return templates.TemplateResponse(
            "approval_error.html",
            {"request": request, "error": result["error"], "request_id": request_id},
        )

    if action == "approve":
        return templates.TemplateResponse(
            "approval_success.html",
            {"request": request, "request_type": request_type, "request_id": request_id, "result": result},
        )
    else:
        return templates.TemplateResponse(
            "approval_rejected.html",
            {"request": request, "request_type": request_type, "request_id": request_id},
        )
