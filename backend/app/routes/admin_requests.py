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
                                   the new-request dialog needs;
* GET  /admin/requests/{type}/{id}/settle - what settling an approved request
                                   that never moved a balance would do (#400);
* POST /admin/requests/{type}/{id}/settle - settle it: take it off now, already
                                   taken off by hand, or no balance needed.
"""

import logging
from datetime import date

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.config import settings
from app.database import async_session
from app.models.request_deletion import RequestDeletion
from app.repositories.postgres.staff import is_archived   # refuse an archived person a new request (#119)
from app.repositories.request_store import request_store
from app.services import balance_settle   # settle an approved request that never moved a balance (#400)
# The same creation, validation, preview and approval pieces every other way
# of making a request uses, so an admin-made request is identical to a normal one.
# _format_balances and _resolve_managers are the dashboard router's own helpers;
# imported by name here so the preview and options endpoints shape data exactly
# as the dashboard does (they are internal to that module, hence the leading _).
from app.routes.dashboard import _format_balances, _resolve_managers
from app.services.carryover_payout import (
    approve_carryover_payout,
    notify_manager_after_failed_admin_approval as recover_carryover_payout,
    refund_carryover_payout,
)
from app.services.employee import get_employee_by_id
from app.services.idempotency import claim_action
from app.services.leave_requests import (
    ALLOWED_LEAVE_TYPES,
    DAY_COSTING_LEAVE_TYPES,
    approve_leave_request,
    notify_manager_after_failed_admin_approval as recover_leave,
    refund_leave_request,
)
from app.services.overtime_requests import (
    approve_overtime_request,
    notify_manager_after_failed_admin_approval as recover_overtime,
    refund_overtime_request,
)
from app.services.request_intake import RequestFormError, parse_request_form, submit_request
from app.services.request_preview import preview_request
from app.services.request_submitter import SOURCE_ADMIN
from app.services.requests_export import build_requests_workbook, requests_export_filename
from app.repositories.staff_store import staff_store  # Staff Directory reads/writes follow staff_storage
from app.services import staff_changes  # records HR balance edits in the change history (#407)
from app.services.balance import recalculate_request_allow_date  # recompute the booking limit after the fix (#407)
from app.services.concurrency import lock_manager  # serialise one person's balance writes (#407)

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
# When an admin's create-and-approve cannot approve, these turn the request back
# into an ordinary Pending one the manager is asked about (#294 S3).
RECOVER_HANDLERS = {
    "leave": recover_leave,
    "overtime": recover_overtime,
    "carryover-payout": recover_carryover_payout,
}

# The leave types the new-request dialog offers, in a stable, sensible order;
# the set itself is the balance engine's ALLOWED_LEAVE_TYPES (the source of truth).
_LEAVE_TYPE_ORDER = ["Vacation", "Sick or Personal Day", "Half Day or Partial Day Off", "Bereavement", "Jury Duty", "Work from Home"]


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


class SettleBody(BaseModel):
    """What one step of the settle dialog posts (#400)."""

    outcome: str                      # "now", "hand" or "none"
    reason: str                       # required, never blank
    pieces: dict[str, float] | None = None   # "hand" leave only: {pot: days taken}


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
        {"status", "request_type", "item_id"} and, when an automatic approval
        did not go through, a "message" for the admin. status is "submitted" for
        a Pending request, "approved" when the admin's approval succeeded, or
        "pending" when approval failed and the request was handed to the manager
        instead (#294 S3).

    Raises:
        HTTPException: 503 while processing is off; 400 for a bad form or an
            unknown status; 404 when the employee is not in the Staff Directory;
            502 when SharePoint refused the write.
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
    # An archived person has left: a request cannot be made for them (#119). They
    # are not in the picker, so this guards a stale id or a crafted call.
    if is_archived(employee.get("fields", {})):
        raise HTTPException(
            status_code=409,
            detail="That person is archived, so a request cannot be made for them.",
        )

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
    item_id = item.get("id")
    result = await _approve_admin_request(body.request_type, item_id)

    # Only a genuine "approved" result counts: approve_carryover_payout can also
    # return {"status": "system_override_rejected"}, which must not read as a
    # success (#294 S2). Anything else is a failure handled below.
    if result.get("status") == "approved":
        return {"status": "approved", "request_type": body.request_type, "item_id": item_id}

    # The approval did not go through (a clash, a system override, or days that
    # could not be worked out). Rather than leave the request silently Pending
    # with its manager email suppressed, hand it to the manager the normal way
    # and tell the admin, so nothing is orphaned and Create is not pressed twice (#294 S3).
    asked = await RECOVER_HANDLERS[body.request_type](item_id)
    if asked:
        message = (
            "This could not be approved automatically, so it was saved as pending "
            "and sent to the manager to approve."
        )
    else:
        # Terminal: the system rejected it, or it is gone. Report the reason; the
        # request (now rejected) still shows on reload.
        message = result.get("error") or "This could not be approved."
    return {
        "status": "pending",
        "request_type": body.request_type,
        "item_id": item_id,
        "message": message,
    }


async def _approve_admin_request(request_type: str, item_id) -> dict:
    """Approve an admin-created request as the admin, with one extra guard.

    process_new_leave_request logs and swallows a day-calculation failure, so a
    day-costing leave (vacation, sick, half day) could reach approval with no
    days worked out and be approved costing nothing. Re-read the item and refuse
    that, so it falls into the S3 fallback and the manager is asked instead (#294 S4).

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        item_id: The created item's id.

    Returns:
        The approval handler's result, or an {"error": ...} refusal that the
        caller turns into the S3 fallback.
    """
    if request_type == "leave":
        fresh = await request_store.get_list_item_or_none(LIST_ID_FOR_TYPE["leave"], item_id)
        ff = fresh["fields"] if fresh else {}                  # the item as created, with its days
        if ff.get("LeaveType") in DAY_COSTING_LEAVE_TYPES and float(ff.get("Days", 0) or 0) <= 0:
            return {"error": "The number of days could not be worked out, so this was not approved."}
    # channel="admin_dashboard"; admin creates and approves in one step here.
    return await APPROVE_HANDLERS[request_type](item_id, ADMIN_ID, "admin_dashboard")


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


class FixNegativeCarryoverBody(BaseModel):
    """What the carry-over fix dialog posts: the required reason for the change."""

    reason: str                       # recorded on the staff change history; never blank


@router.post("/admin/staff/{employee_id}/fix-negative-carryover")
async def admin_fix_negative_carryover(employee_id: str, body: FixNegativeCarryoverBody):
    """Move a person's negative carry-over onto vacation (C -> 0, V += C) (#407).

    Carry-over should never be negative; a stored negative is old data. This
    clears it to zero and folds the shortfall into vacation (which may itself go
    negative, as it is the balance backstop), records the move in the staff
    change history with the required reason, like any other HR balance edit, and
    recomputes the person's booking limit. The write goes through the staff
    store, so it lands in SharePoint or Postgres per the storage setting.

    Args:
        employee_id: Staff Directory id of the person to correct.
        body: The required reason for the change.

    Returns:
        {"status": "fixed", "employee_id", "name", "before", "after",
        "history_recorded"}, each side carrying the vacation and carry-over
        values, plus a "notice" string when a later step (history or the
        booking-limit recompute) failed after the balance was already saved.

    Raises:
        HTTPException: 503 while processing is off; 400 for a blank reason or when
            carry-over is not negative; 404 when the employee is unknown.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    reason = (body.reason or "").strip()                       # a blank reason can never record history
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required.")

    async with lock_manager.lock(str(employee_id)):           # one person's balance writes, serialised
        employee = await get_employee_by_id(employee_id)       # read the current balances under the lock
        if not employee:
            raise HTTPException(status_code=404, detail="That employee is not in the Staff Directory.")
        fields = employee["fields"]
        name = fields.get("Title", "")
        carry_over = float(fields.get("CarryOver", 0) or 0)    # the negative to move
        vacation = float(fields.get("CurrentVacationBalance", 0) or 0)
        if carry_over >= 0:                                    # nothing to fix: do not write or log
            raise HTTPException(status_code=400, detail="Carry-over is not negative, so there is nothing to fix.")

        new_vacation = vacation + carry_over                   # C is negative, so this lowers vacation
        # Include Title so the SharePoint patch does not clear it (store convention).
        await staff_store.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentVacationBalance": new_vacation, "CarryOver": 0.0, "Title": name},
        )
        # The balance is saved now; the history and the booking-limit recompute are
        # best-effort, exactly as the HR entitlements edit treats them. A failure
        # in either must not 500 the request or undo the save: it is logged and
        # reported back in the result so the caller knows what degraded (#407 review).
        history_recorded = True                                # assume success until it throws
        notice = None                                          # extra message only on a failure
        try:
            # One history row per field moved, with the reason (same as HR balance edits).
            await staff_changes.record_changes(
                str(employee_id), name,
                [("CurrentVacationBalance", vacation, new_vacation), ("CarryOver", carry_over, 0.0)],
                reason,
            )
        except Exception:                                      # history must not undo the save
            logger.exception("Failed to record carry-over fix history for %s", employee_id)
            history_recorded = False                           # tell the caller it is missing
            notice = "The change was saved, but its history could not be recorded."
        try:
            # Vacation and carry-over both moved, so recompute the booking limit.
            await recalculate_request_allow_date(employee_id, new_vacation, 0.0)
        except Exception:                                      # a stale derived date is not fatal
            logger.exception("Failed to recalculate request-allow date for %s", employee_id)
            extra = "The can-book-vacation date may be out of date."
            notice = f"{notice} {extra}" if notice else extra

    result = {
        "status": "fixed",
        "employee_id": str(employee_id),
        "name": name,
        "before": {"vacation": vacation, "carryover": carry_over},
        "after": {"vacation": new_vacation, "carryover": 0.0},
        "history_recorded": history_recorded,
    }
    if notice:                                                 # only present when something degraded
        result["notice"] = notice
    return result


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
            a requested refund was refused; 502 when the delete itself failed
            (a refund, if any, has already run).
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

    # Block an in-flight approval of a still-pending item: an approval claims
    # "approve" before it writes, so taking that claim here first stops a
    # manager's approve from running against an item we are about to delete.
    # Harmless on an item that is not pending (#294 S5).
    if fields.get("Status") == "Pending":
        await claim_action(list_id, request_id, "approve")

    # Refund first, only for an approved item the admin chose to refund. A refused
    # refund stops here, so nothing is deleted and no record is kept. Refund must
    # come before the delete: it reads the request and its ledger rows to work out
    # what to reverse, and on Postgres the delete sets those ledger rows'
    # request_id to null (finding 4), so a deleted request could no longer be
    # refunded. If the refund succeeds but the delete below then fails, the item
    # is left refunded and still present (not lost): the admin can simply retry
    # the delete, and the refund is idempotent, so no balance moves twice.
    refunded = False
    if body.refund and fields.get("Status") == "Approved":
        # channel="admin_dashboard"; refund-then-delete is an admin-only path.
        result = await REFUND_HANDLERS[request_type](request_id, ADMIN_ID, "admin_dashboard")
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        refunded = True

    # Delete the item, then keep the record, so a deletion record never outlives
    # an item that is still there. On Postgres the request's child rows follow the
    # foreign-key rules set in Alembic 0021: its timeline and field history cascade
    # away, while balance_ledger and outbox rows survive with their link nulled, so
    # balance history and sent-message records outlive the delete. A 404 means it
    # is already gone, which is success (#294 S5).
    try:
        await request_store.delete_list_item(list_id, request_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            logger.exception("Admin delete: could not remove %s request #%s", request_type, request_id)
            raise HTTPException(
                status_code=502, detail="The request could not be deleted. Try again shortly.",
            ) from e

    # Keep the record now the item is gone.
    async with async_session() as session:
        session.add(RequestDeletion(
            request_type=request_type,
            list_item_id=str(request_id),
            fields=fields,
            reason=reason,
            refunded=refunded,
        ))
        await session.commit()

    logger.info("Admin deleted %s request #%s (refunded=%s)", request_type, request_id, refunded)
    return {"status": "deleted", "request_type": request_type, "request_id": str(request_id), "refunded": refunded}


# The url's request type to the stored kind the settle service reads.
SETTLE_KIND = {"leave": "leave", "overtime": "overtime", "carryover-payout": "carryover_payout"}


def _settle_kind(request_type: str) -> str:
    """The stored kind for a url request type, or a 400.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".

    Returns:
        The stored kind.

    Raises:
        HTTPException: 400 for an unknown type.
    """
    kind = SETTLE_KIND.get(request_type)
    if kind is None:
        raise HTTPException(status_code=400, detail="Invalid request type")
    return kind


@router.get("/admin/requests/{request_type}/{request_id}/settle")
async def admin_settle_preview(request_type: str, request_id: str):
    """What settling one approved, never-applied request would do. Writes nothing.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        request_id: The item id.

    Returns:
        The request, the person's pots now, and the planned moves (see
        ``balance_settle.settle_preview``).

    Raises:
        HTTPException: 400 bad type, 404 gone, 409 not settleable.
    """
    try:
        return await balance_settle.settle_preview(_settle_kind(request_type), request_id)
    except balance_settle.SettleError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/admin/requests/{request_type}/{request_id}/settle")
async def admin_settle_request(request_type: str, request_id: str, body: SettleBody):
    """Settle one approved request that never moved a balance (#400).

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        request_id: The item id.
        body: outcome ("now", "hand", "none"), the required reason, and for a
            "hand" leave the days taken per pot.

    Returns:
        ``{"status": "settled", "outcome", "balance_state", "moves"}``.

    Raises:
        HTTPException: 400 bad input, 404 gone, 409 already settled or not
            possible, 503 while processing is off.
    """
    kind = _settle_kind(request_type)
    if not settings.PROCESSING_ENABLED:                        # a balance write, like every other admin action
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    try:
        result = await balance_settle.settle(kind, request_id, body.outcome, body.reason, body.pieces)
    except balance_settle.SettleError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    logger.info("Admin settled %s request #%s: %s", request_type, request_id, result["outcome"])
    return result


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
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
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
