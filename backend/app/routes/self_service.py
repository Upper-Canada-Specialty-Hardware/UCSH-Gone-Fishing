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
from app.repositories.postgres.staff import is_archived   # refuse an archived submitter (#119)
from app.repositories.request_store import requests_in_postgres  # gate the status token to the Postgres path (#364)
from app.routes.dashboard import AuthUser, _format_balances
from app.services.employee import get_employee_by_id
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.request_intake import (
    PendingClash,
    RequestFormError,
    ensure_request_type_open,
    parse_request_form,
    submit_request,
)
from app.services.request_preview import preview_request
from app.services.request_status import status_token_for
from app.services.request_submitter import SOURCE_REQUEST_PAGE, requests_linked_by_email

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
        {"status": "submitted", "request_type", "item_id", "status_token"}. The
        status_token lets the page follow this request's progress (#364); it is
        None on the SharePoint path, where there is no Postgres row to follow.

    Raises:
        HTTPException: 400 for a form that cannot become a request, or a
            carry-over/payout request while those are closed; 404 when
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
        await ensure_request_type_open(request_type)           # carry-over/payout only in season (#203)
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))

    employee = await get_employee_by_id(user.user_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Your staff record could not be found.")
    # An archived person (left the company) cannot make a request, even holding an
    # old signed link (#119).
    if is_archived(employee.get("fields", {})):
        raise HTTPException(
            status_code=403,
            detail="This account has been archived and can no longer submit requests. Contact HR.",
        )

    # One request until set up (#442): someone who still has a request waiting to
    # be set up (a new hire not added yet) cannot send a new one until that one is
    # handled. Matched by their email.
    from app.services.held_requests import SUPERVISOR_STATUSES, open_held_request_for
    held_email = employee["fields"].get("EmailAddress", "")
    # Only a request still waiting on a supervisor counts: a failed or stuck
    # release is an admin's to finish and must not block an on-staff person.
    if await open_held_request_for(email=held_email, statuses=SUPERVISOR_STATUSES):
        raise HTTPException(
            status_code=409,
            detail="You already have a request waiting to be set up. You can make "
                   "more once your supervisor has set up your profile.",
        )

    # Without SubmitterEmail (requests still in SharePoint, columns not there),
    # the only link from a request back to its person is the person column,
    # which needs them in the site's user list. Refuse up front rather than
    # create a request nobody is told about. Never the case once requests live
    # in Postgres.
    if not await requests_linked_by_email():
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
    except PendingClash as e:
        # Days held by their own pending request (#518): not a plain refusal. The
        # page shows the sentence and offers to change or cancel that request.
        raise HTTPException(status_code=409, detail={
            "code": "pending_clash", "message": str(e), "pending": e.clashes,
        })
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:  # noqa: BLE001 - a SharePoint failure is reported, not a crash
        logger.exception("Request page: could not create a %s request", request_type)
        raise HTTPException(
            status_code=502,
            detail="Your request could not be saved. Try again in a few minutes.",
        )
    item_id = item.get("id")                                   # str(item_id) on the Postgres path
    # The page can only follow a request that has a Postgres row to read; on the
    # SharePoint path there is none, so mint no token even though the id is numeric,
    # and the page keeps the plain steps (#364).
    status_token = status_token_for(request_type, item_id) if await requests_in_postgres() else None
    return {
        "status": "submitted",
        "request_type": request_type,
        "item_id": item_id,
        # The page polls GET /api/intake/request-status with this to follow the
        # request; None when it cannot be named (the SharePoint path) (#364).
        "status_token": status_token,
    }


@router.post("/me/requests/{request_type}/preview")
async def preview_my_request(user: AuthUser, request_type: str, body: dict):
    """What the form would do to the signed-in employee's balances. Saves nothing.

    Works while processing is off: it only reads the staff record and the
    holiday list, so the request page can show it at any time.

    Args:
        user: From the signed token; uid is the employee's Staff Directory id.
        request_type: "leave", "overtime" or "carryover-payout".
        body: The form, snake_case, exactly as it would be submitted.

    Returns:
        {"days", "current": balances, "projected": balances or None,
        "unchanged", "notes", "warning", "next_year"}; balances use the
        dashboard's keys (vacation_balance, sick_balance, overtime, ...).

    Raises:
        HTTPException: 400 for a form that cannot become a request (the
            page hides the preview); 404 when the staff record is gone.
    """
    try:
        form = parse_request_form(request_type, body)          # same checks as submitting
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))

    employee = await get_employee_by_id(user.user_id)          # one read of the staff record
    if not employee:
        raise HTTPException(status_code=404, detail="Your staff record could not be found.")
    fields = employee["fields"]

    result = await preview_request(request_type, form, fields)
    projected = result.pop("projected")
    return {
        **result,
        "current": _format_balances(fields),
        # Untouched pots (Payout, entitlements) carry over from the record.
        "projected": _format_balances({**fields, **projected}) if projected else None,
    }


# ----- change or cancel your own pending request (#518, #519) -----


async def _signed_in_employee(user: AuthUser) -> dict:
    """The signed-in person's staff record, refused when gone or archived.

    Args:
        user: From the signed token.

    Returns:
        The staff record ({"id", "fields"}).

    Raises:
        HTTPException: 503 while processing is off; 404 when the record is gone;
            403 when it is archived.
    """
    if not settings.PROCESSING_ENABLED:                        # read-only: change nothing, email nobody
        raise HTTPException(status_code=503, detail="This cannot be done right now. Try again later.")
    employee = await get_employee_by_id(user.user_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Your staff record could not be found.")
    if is_archived(employee.get("fields", {})):                # left the company (#119)
        raise HTTPException(status_code=403, detail="This account has been archived. Contact HR.")
    return employee


@router.post("/me/requests/leave/{item_id}/change")
async def change_my_leave(user: AuthUser, item_id: int, body: dict):
    """Change the signed-in person's own pending leave to the form sent (#518).

    The request page calls this from its change-or-cancel offer, with the form the
    person just filled in. The request keeps its number; the manager is asked
    again with fresh links and a line saying what changed, and the person gets a
    fresh receipt, all in the request's one email thread.

    Args:
        user: From the signed token; uid is the staff id the request must belong to.
        item_id: The pending leave request to change.
        body: The leave form, snake_case, as for submitting.

    Returns:
        {"status": "changed", "request_type": "leave", "item_id", "status_token"}.

    Raises:
        HTTPException: 400 for a bad form or nothing changed; 404 when it is not
            theirs; 409 when it is no longer pending or the new days clash; 503
            while processing is off.
    """
    from app.services.own_requests import OwnRequestError, change_own_leave
    try:
        form = parse_request_form("leave", body)               # the same checks as submitting
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))
    employee = await _signed_in_employee(user)
    try:
        result = await change_own_leave(item_id, employee, form)
    except OwnRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    return {
        **result, "request_type": "leave",
        "status_token": status_token_for("leave", item_id),     # the page follows the changed request
    }


@router.post("/me/requests/{request_type}/{item_id}/cancel")
async def cancel_my_request(user: AuthUser, request_type: str, item_id: int, body: dict | None = None):
    """Cancel the signed-in person's own pending request (#519).

    From the request page (the change-or-cancel offer, or the page after sending)
    and from History. Only a pending request: approved time off is refunded by an
    admin. The manager who was asked is told in the request's thread.

    Args:
        user: From the signed token; uid is the staff id the request must belong to.
        request_type: "leave", "overtime" or "carryover-payout".
        item_id: The request number.
        body: Optional ``{"from": "history"}`` to record where it came from.

    Returns:
        {"status": "cancelled", "item_id", "already"}.

    Raises:
        HTTPException: 400 for an unknown type; 404 when it is not theirs; 409
            when it is no longer pending; 503 while processing is off.
    """
    from app.services.own_requests import KIND_FROM_URL, OwnRequestError, cancel_own_request
    kind = KIND_FROM_URL.get(request_type)
    if kind is None:
        raise HTTPException(status_code=400, detail="Unknown request type.")
    employee = await _signed_in_employee(user)
    channel = "employee_dashboard" if (body or {}).get("from") == "history" else "request_page"
    try:
        return await cancel_own_request(kind, item_id, employee["id"], channel=channel)
    except OwnRequestError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))

