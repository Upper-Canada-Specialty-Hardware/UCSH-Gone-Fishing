import logging

from app.config import settings

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
    """Detect if manager was just assigned → trigger approval email."""
    if not fields.get("Managertxt"):
        return
    if fields.get("Status") != "Pending":
        return
    if fields.get("ApproveProcessedFlag") == "Processed":
        return

    from app.services.leave_requests import send_approval_email
    logger.info("Dispatching leave request approval for #%s", item_id)
    await send_approval_email(item_id)


async def _handle_overtime_request_change(item_id: str, fields: dict):
    """Detect if manager was just assigned → trigger approval."""
    if fields.get("Status") != "Pending":
        return

    manager = fields.get("Manager")
    if not manager:
        return

    from app.services.overtime_requests import send_approval_email
    from app.services.employee import get_employee_by_name, get_all_managers_for_employee

    submitted_by = fields.get("SubmittedBy", {})
    submitter_name = submitted_by.get("LookupValue", "") if isinstance(submitted_by, dict) else ""
    employee = await get_employee_by_name(submitter_name)
    if not employee:
        return

    managers = await get_all_managers_for_employee(employee)
    if not managers:
        return

    logger.info("Dispatching overtime approval for #%s", item_id)
    await send_approval_email(item_id, employee, managers)


async def _handle_carryover_payout_change(item_id: str, fields: dict):
    """Detect if manager was just assigned → trigger approval pipeline."""
    if not fields.get("Managertxt"):
        return
    if fields.get("SystemState") != "Not Processed":
        return

    from app.services.carryover_payout import run_approval_pipeline
    logger.info("Dispatching carryover/payout approval for #%s", item_id)
    await run_approval_pipeline(item_id)
