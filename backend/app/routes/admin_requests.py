"""Admin requests screen: new request, delete, balance preview and export (#278).

These are the write and preview endpoints the rebuilt admin requests table
needs, kept in their own module so the large dashboard router does not grow.
Every route sits under /api/dashboard behind the admin password, exactly like
the other /admin/* routers in app/main.py.

The existing admin actions (approve, reject, refund, send reminder, reprocess,
edit, the held-request actions) stay where they are in routes/dashboard.py; the
table calls them unchanged. Only the genuinely new behaviour lives here:

* POST /admin/requests/new      - create a request as an admin (Pending, or
                                   approved on the spot without asking the manager);
* POST /admin/requests/{type}/{id}/delete - delete a request, with a reason and
                                   an optional refund, keeping a deletion record;
* POST /admin/requests/preview  - what a new request would do to balances;
* POST /admin/requests/export   - an .xlsx of the requests the admin is viewing;
* GET  /admin/requests/new-options - the Staff Directory people and leave types
                                   the new-request dialog needs.
"""

import logging
from datetime import date

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.request_deletion import RequestDeletion
from app.repositories.request_store import request_store
# The same creation, validation, preview and approval pieces every other way
# of making a request uses, so an admin-made request is identical to a normal one.
from app.routes.dashboard import _format_balances, _resolve_managers
from app.services.carryover_payout import approve_carryover_payout, refund_carryover_payout
from app.services.employee import get_employee_by_id
from app.services.leave_requests import ALLOWED_LEAVE_TYPES, approve_leave_request, refund_leave_request
from app.services.overtime_requests import approve_overtime_request, refund_overtime_request
from app.services.request_intake import RequestFormError, parse_request_form, submit_request
from app.services.request_preview import preview_request
from app.services.request_submitter import SOURCE_ADMIN
from app.services.requests_export import build_requests_workbook, requests_export_filename

logger = logging.getLogger(__name__)
router = APIRouter()

# Who an admin action is recorded as; get_employee_by_id("admin") is None, so
# the approval handlers treat it as an admin with no staff record (same value
# the dashboard router's admin approve/reject/refund already pass).
ADMIN_ID = "admin"

# The three request types, each to its SharePoint list id and its handlers.
LIST_ID_FOR_TYPE = {
    "leave": settings.SP_LIST_LEAVE_REQUESTS,
    "overtime": settings.SP_LIST_OVERTIME_REQUESTS,
    "carryover-payout": settings.SP_LIST_CARRYOVER_PAYOUT,
}
APPROVE_HANDLERS = {
    "leave": approve_leave_request,
    "overtime": approve_overtime_request,
    "carryover-payout": approve_carryover_payout,
}
REFUND_HANDLERS = {
    "leave": refund_leave_request,
    "overtime": refund_overtime_request,
    "carryover-payout": refund_carryover_payout,
}

# The leave types the new-request dialog offers, in a stable, sensible order;
# the set itself is the balance engine's ALLOWED_LEAVE_TYPES (the source of truth).
_LEAVE_TYPE_ORDER = ["Vacation", "Sick or Personal Day", "Half Day or Partial Day Off", "Bereavement", "Jury Duty"]


class NewRequestBody(BaseModel):
    """What the new-request dialog posts.

    The form fields (leave_type, start_date, dates, hours, type_of_request,
    days, ...) travel in ``form`` and are checked by parse_request_form, the
    same validation the request page uses, so an admin-made request obeys the
    same rules as any other.
    """

    request_type: str                 # "leave", "overtime" or "carryover-payout"
    employee_id: str                  # Staff Directory id of the person it is for
    status: str = "Pending"           # "Pending" (ask the manager) or "Approved" (do it now)
    form: dict                        # the request form, snake_case


class PreviewBody(BaseModel):
    """What the new-request dialog posts to see the balance effect before saving."""

    request_type: str
    employee_id: str
    form: dict


class DeleteBody(BaseModel):
    """What the delete dialog posts."""

    reason: str                       # required, never blank
    refund: bool = False              # True: reverse an approved item's balance change first


class ExportBody(BaseModel):
    """What the Export command posts: the request items currently on screen."""

    requests: list[dict]


@router.post("/admin/requests/new")
async def admin_new_request(body: NewRequestBody):
    """Create a request on behalf of an employee from the admin dashboard.

    Pending works exactly like a normal form submission: the request is created,
    its days and manager are filled in, and the manager is emailed to approve
    it. Approved creates the request the same way but skips that email, then
    runs the normal approval handler as the admin, so balances are updated, the
    audit trail is written and the usual approval emails go out, and the manager
    is never asked to approve something already decided.

    Carry-over and payout requests made here are allowed outside the carry-over
    date window: that window gates what staff may ask for themselves, but this
    is HR's own tool, so ensure_request_type_open is deliberately not called.

    Args:
        body: request_type, employee_id, status ("Pending"/"Approved") and the
            form, snake_case (see services/request_intake.py).

    Returns:
        {"status": "submitted"|"approved", "request_type", "item_id"}.

    Raises:
        HTTPException: 503 while processing is off; 400 for a bad form, an
            unknown status, or a refused approval; 404 when the employee is not
            in the Staff Directory; 502 when SharePoint refused the write.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    status = body.status                                       # "Pending" or "Approved"
    if status not in ("Pending", "Approved"):
        raise HTTPException(status_code=400, detail="Status must be Pending or Approved.")

    try:
        form = parse_request_form(body.request_type, body.form)   # same checks as every other submission
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))

    employee = await get_employee_by_id(body.employee_id)      # the person it is for
    if not employee:
        raise HTTPException(status_code=404, detail="That employee is not in the Staff Directory.")

    # Pending notifies the manager; Approved suppresses that email and approves below.
    notify = status == "Pending"
    try:
        item = await submit_request(body.request_type, form, employee, SOURCE_ADMIN, notify=notify)
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:  # noqa: BLE001 - a SharePoint failure is reported, not a crash
        logger.exception("Admin new request: could not create a %s request", body.request_type)
        raise HTTPException(status_code=502, detail="The request could not be saved. Try again shortly.")

    if status == "Pending":
        return {"status": "submitted", "request_type": body.request_type, "item_id": item.get("id")}

    # Approved: run the request through the real approval handler as the admin.
    approve = APPROVE_HANDLERS[body.request_type]
    result = await approve(item["id"], ADMIN_ID)
    if "error" in result:
        # The item exists but could not be approved (e.g. a clash with approved
        # leave); surface the reason rather than leave it silently Pending.
        raise HTTPException(status_code=400, detail=result["error"])
    return {"status": "approved", "request_type": body.request_type, "item_id": item.get("id")}


@router.post("/admin/requests/preview")
async def admin_preview_request(body: PreviewBody):
    """What a new request would do to the chosen employee's balances. Saves nothing.

    Reads only the staff record and the holiday list, so it answers even while
    processing is off (the dialog shows the effect before HR decides to save).

    Args:
        body: request_type, employee_id and the form, snake_case.

    Returns:
        {"days", "current": balances, "projected": balances or None,
        "unchanged", "notes", "warning", "next_year"}, balances in the
        dashboard's keys (vacation_balance, sick_balance, ...).

    Raises:
        HTTPException: 400 for a form that cannot become a request; 404 when
            the employee is not in the Staff Directory.
    """
    try:
        form = parse_request_form(body.request_type, body.form)
    except RequestFormError as e:
        raise HTTPException(status_code=400, detail=str(e))

    employee = await get_employee_by_id(body.employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="That employee is not in the Staff Directory.")
    fields = employee["fields"]

    result = await preview_request(body.request_type, form, fields)
    projected = result.pop("projected")                        # SharePoint-shaped; map to the dashboard keys below
    return {
        **result,
        "current": _format_balances(fields),
        "projected": _format_balances({**fields, **projected}) if projected else None,
    }


@router.post("/admin/requests/{request_type}/{request_id}/delete")
async def admin_delete_request(request_type: str, request_id: str, body: DeleteBody):
    """Delete one request, with a required reason and an optional refund (#282).

    The item is removed for good, so what it was is kept in request_deletions
    first: the whole field set, the reason, and whether its balance change was
    refunded. An approved item can be refunded before deletion, which runs the
    same refund handler /admin/refund uses, so balances end up as if the request
    never happened.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        request_id: The item id.
        body: reason (required, non-blank) and refund (reverse an approved
            item's balance change first).

    Returns:
        {"status": "deleted", "request_type", "request_id", "refunded"}.

    Raises:
        HTTPException: 400 for an unknown type or a blank reason; 503 while
            processing is off; 404 when the request no longer exists; 400 when
            a requested refund was refused.
    """
    list_id = LIST_ID_FOR_TYPE.get(request_type)
    if not list_id:
        raise HTTPException(status_code=400, detail="Invalid request type")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required.")
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    item = await request_store.get_list_item_or_none(list_id, request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="This request no longer exists.")
    fields = dict(item.get("fields", {}))                      # the state at deletion, kept in the record

    # Refund first, only for an approved item the admin chose to refund.
    refunded = False
    if body.refund and fields.get("Status") == "Approved":
        result = await REFUND_HANDLERS[request_type](request_id, ADMIN_ID)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        refunded = True

    # Keep the record before the item is gone.
    async with async_session() as session:
        session.add(RequestDeletion(
            request_type=request_type,
            list_item_id=str(request_id),
            fields=fields,
            reason=reason,
            refunded=refunded,
        ))
        await session.commit()

    await request_store.delete_list_item(list_id, request_id)
    logger.info("Admin deleted %s request #%s (refunded=%s)", request_type, request_id, refunded)
    return {"status": "deleted", "request_type": request_type, "request_id": str(request_id), "refunded": refunded}


@router.post("/admin/requests/export")
async def admin_export_requests(body: ExportBody):
    """An .xlsx of the requests the admin is viewing, one sheet per list (#278).

    Formats the request items the dashboard already holds (the shown rows, or
    the ticked ones), so it reads no live data and works at any time.

    Args:
        body: requests - the items as /admin/requests returns them.

    Returns:
        The workbook as an attachment.
    """
    data = build_requests_workbook(body.requests)
    filename = requests_export_filename(date.today())
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/requests/new-options")
async def admin_new_request_options():
    """The Staff Directory people and leave types the new-request dialog needs.

    Returns:
        {"staff": [{"id", "name", "department", "location", "email",
        "managers"}], "leave_types": [...]}, staff sorted by name.
    """
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    staff = []
    for item in items:
        f = item.get("fields", {})
        name = f.get("Title", "")
        if not name:
            continue                                           # a record with no name cannot be picked
        staff.append({
            "id": str(item.get("id")),
            "name": name,
            "department": f.get("Department", ""),
            "location": f.get("Location", ""),
            "email": f.get("EmailAddress", ""),
            "managers": _resolve_managers(item),               # "A, B" from AllManagers, or ""
        })
    staff.sort(key=lambda s: s["name"].lower())
    # Known types first, then any other allowed type (so a new one still shows).
    leave_types = [t for t in _LEAVE_TYPE_ORDER if t in ALLOWED_LEAVE_TYPES]
    leave_types += sorted(t for t in ALLOWED_LEAVE_TYPES if t not in leave_types)
    return {"staff": staff, "leave_types": leave_types}
