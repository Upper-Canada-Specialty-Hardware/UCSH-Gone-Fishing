"""A signed-in employee submits a request from the public request page.

POST /api/dashboard/me/requests/{request_type}, with the same signed token
query parameters (token, role, uid, exp) as every other /me endpoint. The
request page gets that token from the emailed code (routes/intake.py), so
the submitter is whoever proved they own the email on their staff record;
nothing in the body can say otherwise.

Mounted under /api/dashboard next to the dashboard router, in its own module
to keep that file from growing further.
"""

import logging

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.routes.dashboard import AuthUser
from app.services.employee import get_employee_by_id
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.request_intake import RequestFormError, parse_request_form, submit_request
from app.services.request_submitter import SOURCE_REQUEST_PAGE

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/me/requests/{request_type}")
async def submit_my_request(user: AuthUser, request_type: str, body: dict):
    """Create a leave, overtime or carryover/payout request for the signed-in employee.

    Args:
        user: From the signed token; uid is the employee's Staff Directory id.
        request_type: "leave", "overtime" or "carryover-payout".
        body: The form, snake_case (see services/request_intake.py).

    Returns:
        {"status": "submitted", "request_type", "item_id"}.

    Raises:
        HTTPException: 400 for a form that cannot become a request; 404 when
            the staff record is gone; 409 when the request could not be routed
            to anyone yet; 503 while processing is off; 502 when SharePoint
            refused the write.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Requests cannot be submitted right now. Try again later.",
        )
    try:
        form = parse_request_form(request_type, body)          # checked before any SharePoint call
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))

    employee = await get_employee_by_id(user.user_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Your staff record could not be found.")

    # Without the SubmitterEmail column, the only link from a request back to
    # its person is the person column, which needs them in the site's user
    # list. Refuse up front rather than create a request nobody is told about.
    if not settings.REQUEST_EMAIL_COLUMNS_ENABLED:
        email = employee["fields"].get("EmailAddress", "")
        if not await _resolve_user_lookup_id(email):
            raise HTTPException(
                status_code=409,
                detail="Your account is not linked to the SharePoint site yet, so this "
                       "request cannot reach your manager. Ask your manager to submit it "
                       "for you, or try again in a few days.",
            )

    try:
        item = await submit_request(request_type, form, employee, SOURCE_REQUEST_PAGE)
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:  # noqa: BLE001 - a SharePoint failure is reported, not a crash
        logger.exception("Request page: could not create a %s request", request_type)
        raise HTTPException(
            status_code=502,
            detail="Your request could not be saved. Try again in a few minutes.",
        )
    return {"status": "submitted", "request_type": request_type, "item_id": item.get("id")}
