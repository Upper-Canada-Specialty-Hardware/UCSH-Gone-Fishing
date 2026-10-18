import logging

from app.config import settings
from app.repositories.request_store import is_request_list, request_store, requests_in_postgres

logger = logging.getLogger(__name__)


async def dispatch_change(list_id: str, item: dict):
    """Route SP list changes to the correct service pipeline.

    Once requests live in Postgres (#335), a SharePoint request list is no longer
    a live source: the webhook path stops treating its changes as requests and
    leaves them to the scheduled SharePoint check (services/sharepoint_check.py),
    which imports a new Form submission, and flags any other new item, edit or
    delete for review. So a request-list change here is ignored after the switch.

    Args:
        list_id: The SharePoint list the change came from.
        item: The changed item as SharePoint sent it.
    """
    # While the storage switch is pausing writes or running its final sync, a
    # request-list change must not be auto-processed: a new approval or a computed
    # day count would land in SharePoint after the final sync has read it and be
    # lost. The scheduled SharePoint check picks the item up afterwards, so skip and
    # log it here rather than process it (#335 fix).
    if is_request_list(list_id):
        from app.services.storage_switch import switch_is_running
        from app.services.switch_pause import is_paused
        if switch_is_running() or await is_paused():
            logger.info("Storage switch in progress; skipping SharePoint %s item %s", list_id, item.get("id"))
            return

    if await requests_in_postgres() and is_request_list(list_id):
        # The request list is no longer the live source. Anything that still lands
        # there (the Form until it is turned off, or a hand edit) is never imported
        # as a request: the per-item check flags it for an admin instead. This is
        # true whether or not staff have also moved (a half switch through an env
        # override changes nothing here, because nothing is imported or notified);
        # the webhook is logged so a stray item is never dropped in silence
        # (#336 review, finding 10).
        from app.services.sharepoint_check import run_sharepoint_check_for_item
        logger.info("Requests are in Postgres; flagging SharePoint %s item %s via the check", list_id, item.get("id"))
        await run_sharepoint_check_for_item(list_id, item.get("id"))
        return
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

    if fields.get("ManagerLookupId"):
        # Manager already assigned — existing path: send approval email
        if fields.get("ApproveProcessedFlag") == "Processed":
            return
        from app.services.leave_requests import send_approval_email
        logger.info("Dispatching leave request approval for #%s", item_id)
        await send_approval_email(item_id)
        return

    # No ManagerLookupId → SP-created item, auto-process it
    item = await request_store.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, item_id)
    f = item["fields"]
    if not f.get("StartDate") or not f.get("EndDate"):
        logger.warning(
            "Leave #%s missing StartDate/EndDate — skipping auto-process", item_id
        )
        return

    # Every pending request now runs the three steps below and reaches a manager.
    # The duplicate check that used to sit here rejected the request and returned
    # before any of them, which is why such requests carried no days, no manager
    # and no location. Clashes with an approved absence are raised at approval
    # instead — see app/services/overlap_detection.py.

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
        from app.services.employee import get_all_managers_for_employee
        from app.services.request_submitter import resolve_request_submitter

        employee = await resolve_request_submitter(fields, "SubmittedBy")
        if not employee:
            return

        managers = await get_all_managers_for_employee(employee)
        if not managers:
            return

        logger.info("Dispatching overtime approval for #%s", item_id)
        await send_approval_email(item_id, employee, managers)
        return

    # No Manager → SP-created item, auto-process it
    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, item_id)
    f = item["fields"]
    if not f.get("Hours"):
        logger.warning("Overtime #%s missing Hours — skipping auto-process", item_id)
        return

    # As with leave: no duplicate check here, so processing always continues to
    # manager assignment. Clashes are raised at approval time instead.

    # Resolve submitter email from SubmittedBy Person field via Staff Directory
    submitter_email = None
    from app.services.request_submitter import resolve_request_submitter
    emp = await resolve_request_submitter(f, "SubmittedBy")
    if emp:
        submitter_email = emp["fields"].get("EmailAddress", "")

    from app.services.overtime_requests import auto_assign_manager
    logger.info("Auto-processing SP-created overtime request #%s", item_id)
    await auto_assign_manager(item_id, submitter_email=submitter_email)


async def _handle_carryover_payout_change(item_id: str, fields: dict):
    """Detect manager assignment or auto-process SP-created items."""
    if fields.get("ManagerLookupId"):
        # Manager already assigned — existing path: run approval pipeline
        if fields.get("SystemState") != "Not Processed":
            return
        from app.services.carryover_payout import run_approval_pipeline
        logger.info("Dispatching carryover/payout approval for #%s", item_id)
        await run_approval_pipeline(item_id)
        return

    # No ManagerLookupId → SP-created item, auto-process it
    system_state = fields.get("SystemState")
    if system_state and system_state != "Not Processed":
        return

    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, item_id)
    f = item["fields"]
    if not f.get("Days"):
        logger.warning("Carryover #%s missing Days — skipping auto-process", item_id)
        return

    # Resolve submitter email from SubmittedBy Person field via Staff Directory
    submitter_email = None
    from app.services.request_submitter import resolve_request_submitter
    emp = await resolve_request_submitter(f, "SubmittedBy")
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
