import logging

from app.config import settings
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)


async def dispatch_change(list_id: str, item: dict):
    """Route SP list changes to the correct service pipeline."""
    fields = item.get("fields", {})
    item_id = item.get("id")

    if list_id == settings.SP_LIST_LEAVE_REQUESTS:
        await _handle_leave_request_change(item_id, fields)
    elif list_id == settings.SP_LIST_OVERTIME_REQUESTS:
        await _handle_overtime_request_change(item_id, fields)
    elif list_id == settings.SP_LIST_CARRYOVER_PAYOUT:
        await _handle_carryover_payout_change(item_id, fields)
    elif list_id == settings.SP_LIST_COMPANY_HOLIDAYS:
        logger.info("Company Holidays list changed — no automatic action required")
    else:
        logger.debug("Ignoring change notification for unknown list: %s", list_id)


async def _handle_leave_request_change(item_id: str, fields: dict):
    """Detect manager assignment or auto-process SP-created items."""
    if fields.get("Status") != "Pending":
        return

    if fields.get("Managertxt"):
        # Manager already assigned — existing path: send approval email
        if fields.get("ApproveProcessedFlag") == "Processed":
            return
        from app.services.leave_requests import send_approval_email
        logger.info("Dispatching leave request approval for #%s", item_id)
        await send_approval_email(item_id)
        return

    # No Managertxt → SP-created item, auto-process it
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, item_id)
    f = item["fields"]
    if not f.get("StartDate") or not f.get("EndDate"):
        logger.warning(
            "Leave #%s missing StartDate/EndDate — skipping auto-process", item_id
        )
        return

    # Duplicate detection — auto-reject overlapping requests
    from app.services.overlap_detection import check_leave_overlap, _extract_lookup_id
    submitter_lid = _extract_lookup_id(f, "SubmittedTest")
    if submitter_lid:
        overlap = await check_leave_overlap(
            submitter_lookup_id=submitter_lid,
            start_date=f.get("StartDate", ""),
            end_date=f.get("EndDate", ""),
            exclude_item_id=str(item_id),
        )
        if overlap:
            await _auto_reject_duplicate(item_id, "leave", f, overlap)
            return

    from app.services.leave_requests import (
        auto_calculate_days,
        auto_assign_manager,
        send_bereavement_alert,
    )

    logger.info("Auto-processing SP-created leave request #%s", item_id)
    await auto_calculate_days(item_id)
    await auto_assign_manager(item_id)
    await send_bereavement_alert(item_id)


async def _handle_overtime_request_change(item_id: str, fields: dict):
    """Detect manager assignment or auto-process SP-created items."""
    if fields.get("Status") != "Pending":
        return

    manager = fields.get("ManagerLookupId")
    if manager:
        # Manager already assigned — existing path: send approval email
        from app.services.overtime_requests import send_approval_email
        from app.services.employee import resolve_person_field, get_all_managers_for_employee

        employee = await resolve_person_field(fields.get("SubmittedBy") or fields.get("SubmittedByLookupId"))
        if not employee:
            return

        managers = await get_all_managers_for_employee(employee)
        if not managers:
            return

        logger.info("Dispatching overtime approval for #%s", item_id)
        await send_approval_email(item_id, employee, managers)
        return

    # No Manager → SP-created item, auto-process it
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, item_id)
    f = item["fields"]
    if not f.get("Hours"):
        logger.warning("Overtime #%s missing Hours — skipping auto-process", item_id)
        return

    # Duplicate detection — auto-reject overlapping requests
    from app.services.overlap_detection import check_overtime_overlap, _extract_lookup_id
    submitter_lid = _extract_lookup_id(f, "SubmittedBy")
    if submitter_lid:
        overlap = await check_overtime_overlap(
            submitter_lookup_id=submitter_lid,
            overtime_date=f.get("StartDate", ""),
            exclude_item_id=str(item_id),
        )
        if overlap:
            await _auto_reject_duplicate(item_id, "overtime", f, overlap)
            return

    # Resolve submitter email from SubmittedBy Person field via Staff Directory
    submitter_email = None
    from app.services.employee import resolve_person_field
    emp = await resolve_person_field(f.get("SubmittedBy") or f.get("SubmittedByLookupId"))
    if emp:
        submitter_email = emp["fields"].get("EmailAddress", "")

    from app.services.overtime_requests import auto_assign_manager
    logger.info("Auto-processing SP-created overtime request #%s", item_id)
    await auto_assign_manager(item_id, submitter_email=submitter_email)


async def _handle_carryover_payout_change(item_id: str, fields: dict):
    """Detect manager assignment or auto-process SP-created items."""
    if fields.get("Managertxt"):
        # Manager already assigned — existing path: run approval pipeline
        if fields.get("SystemState") != "Not Processed":
            return
        from app.services.carryover_payout import run_approval_pipeline
        logger.info("Dispatching carryover/payout approval for #%s", item_id)
        await run_approval_pipeline(item_id)
        return

    # No Managertxt → SP-created item, auto-process it
    system_state = fields.get("SystemState")
    if system_state and system_state != "Not Processed":
        return

    item = await sp_client.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, item_id)
    f = item["fields"]
    if not f.get("Days"):
        logger.warning("Carryover #%s missing Days — skipping auto-process", item_id)
        return

    # Resolve submitter email from SubmittedBy Person field via Staff Directory
    submitter_email = None
    from app.services.employee import resolve_person_field
    emp = await resolve_person_field(f.get("SubmittedBy") or f.get("SubmittedByLookupId"))
    if emp:
        submitter_email = emp["fields"].get("EmailAddress", "")

    if not submitter_email:
        logger.warning(
            "Carryover #%s — cannot resolve submitter email, skipping auto-process", item_id
        )
        return

    from app.services.carryover_payout import auto_assign_manager
    logger.info("Auto-processing SP-created carryover/payout request #%s", item_id)
    await auto_assign_manager(item_id, submitter_email)


async def _auto_reject_duplicate(item_id: str, request_type: str, fields: dict, overlap: dict):
    """Auto-reject a duplicate request and notify the employee."""
    if request_type == "leave":
        list_id = settings.SP_LIST_LEAVE_REQUESTS
        update_fields = {"Status": "Rejected", "ApproveProcessedFlag": "Processed"}
        person_field = fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId")
    else:
        list_id = settings.SP_LIST_OVERTIME_REQUESTS
        update_fields = {"Status": "Rejected"}
        person_field = fields.get("SubmittedBy") or fields.get("SubmittedByLookupId")

    await sp_client.update_list_item_fields(list_id, item_id, update_fields)
    logger.info(
        "Auto-rejected duplicate %s request #%s — overlaps with #%s",
        request_type, item_id, overlap.get("item_id"),
    )

    # Notify employee
    from app.services.employee import resolve_person_field
    employee = await resolve_person_field(person_field)
    if not employee:
        return
    emp_email = employee["fields"].get("EmailAddress", "")
    if not emp_email:
        return

    from app.templates_render import render_duplicate_request_rejected
    from app.graph.email import send_email

    html = render_duplicate_request_rejected(request_type, fields, overlap)
    await send_email(
        to=[emp_email],
        subject=f"{'Leave' if request_type == 'leave' else 'Time Make-Up'} Request - Auto Rejected (Duplicate)",
        html_body=html,
    )
