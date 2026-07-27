import logging

from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services.approval_links import validate_approval_token
from app.services.approval_versions import get_current_version
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

LIST_ID_FOR_TYPE = {
    "leave": settings.SP_LIST_LEAVE_REQUESTS,
    "overtime": settings.SP_LIST_OVERTIME_REQUESTS,
    "carryover-payout": settings.SP_LIST_CARRYOVER_PAYOUT,
}


def _error_page(request, request_id, message):
    """The one place approval_error.html is rendered.

    Keeping every error path behind a single call means a change to the
    templating call signature is a one-line edit here, not a sweep of the module.
    """
    return templates.TemplateResponse(
        request,
        "approval_error.html",
        {"error": message, "request_id": request_id},
    )


async def _validate_and_check(request, request_type, action, request_id, token, mgr, exp, version):
    """Shared validation for GET and POST. Returns error response or None."""
    if not settings.PROCESSING_ENABLED:
        return _error_page(
            request, request_id,
            "System is currently in reporting-only mode. Approvals are disabled.",
        )

    valid, error_msg = validate_approval_token(
        request_type, request_id, action, mgr, token, exp, version
    )
    if not valid:
        return _error_page(request, request_id, error_msg)

    if (request_type, action) not in HANDLERS:
        return _error_page(request, request_id, "Invalid request type or action")

    list_id = LIST_ID_FOR_TYPE.get(request_type)
    if list_id:
        current_version = await get_current_version(list_id, request_id)
        if version != current_version:
            logger.info(
                "Stale approval link for %s #%s - link v%s, current v%s",
                request_type, request_id, version, current_version,
            )
            return templates.TemplateResponse(
                request,
                "approval_outdated.html",
                {"request_type": request_type, "request_id": request_id},
            )

    return None


@router.get("/{request_type}/{action}/{request_id}", response_class=HTMLResponse)
async def confirm_approval(
    request: Request,
    request_type: str,
    action: str,
    request_id: str,
    token: str = Query(...),
    mgr: str = Query(...),
    exp: str = Query(...),
    v: int = Query(1),
):
    """Show confirmation page - does NOT process the action."""
    error_response = await _validate_and_check(request, request_type, action, request_id, token, mgr, exp, v)
    if error_response:
        return error_response

    return templates.TemplateResponse(
        request,
        "approval_confirm.html",
        {
            "request_type": request_type,
            "action": action,
            "request_id": request_id,
            "token": token,
            "mgr": mgr,
            "exp": exp,
            "v": v,
        },
    )


@router.post("/{request_type}/{action}/{request_id}", response_class=HTMLResponse)
async def handle_approval(
    request: Request,
    request_type: str,
    action: str,
    request_id: str,
    token: str = Form(...),
    mgr: str = Form(...),
    exp: str = Form(...),
    v: int = Form(1),
):
    """Process the approval/rejection action (requires POST from confirmation page)."""
    error_response = await _validate_and_check(request, request_type, action, request_id, token, mgr, exp, v)
    if error_response:
        return error_response

    handler = HANDLERS[(request_type, action)]

    try:
        result = await handler(request_id, mgr)
    except Exception as e:
        logger.exception("Error processing approval for %s #%s", request_type, request_id)
        return _error_page(request, request_id, str(e))

    if "error" in result:
        return _error_page(request, request_id, result["error"])

    if action == "approve":
        return templates.TemplateResponse(
            request,
            "approval_success.html",
            {"request_type": request_type, "request_id": request_id, "result": result},
        )
    else:
        return templates.TemplateResponse(
            request,
            "approval_rejected.html",
            {"request_type": request_type, "request_id": request_id},
        )
