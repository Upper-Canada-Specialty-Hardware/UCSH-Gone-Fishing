import logging
from datetime import date

from app.config import settings
from app.graph.sharepoint import sp_client
from app.graph.email import send_email
from app.services.employee import get_employee_by_email, get_employee_by_id, get_manager_for_employee
from app.services.balance import recalculate_request_allow_date
from app.services.concurrency import lock_manager
from app.services.approval_links import generate_approval_url
from app.services.leave_requests import _resolve_user_lookup_id

logger = logging.getLogger(__name__)


async def process_new_carryover_payout(form_data: dict, submitter_email: str) -> dict:
    """Create SP item → auto-assign manager → pre-validate → send emails."""
    request_type = form_data.get("type_of_request", "")  # "Carry Over" or "Payout"
    days = float(form_data.get("days", 0))

    fields = {
        "TypeofRequest": request_type,
        "Days": days,
        "SystemState": "Not Processed",
    }

    lookup_id = await _resolve_user_lookup_id(submitter_email)
    if lookup_id:
        fields["SubmittedByLookupId"] = lookup_id

    item = await sp_client.create_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, fields)
    item_id = item["id"]
    logger.info("Created carryover/payout request #%s", item_id)

    await auto_assign_manager(item_id, submitter_email)
    return item


async def auto_assign_manager(request_id: str | int, submitter_email: str):
    """Look up employee by email → supervisor → assign manager + IDs."""
    employee = await get_employee_by_email(submitter_email)
    if not employee:
        logger.warning("Cannot assign manager — employee not found for CO/PO #%s", request_id)
        return

    emp_fields = employee["fields"]
    employee_id = employee["id"]

    manager = await get_manager_for_employee(employee)
    if not manager:
        return

    mgr_fields = manager["fields"]
    manager_id = manager["id"]
    mgr_email = mgr_fields.get("EmailAddress", "")
    manager_lookup_id = await _resolve_user_lookup_id(mgr_email)

    update = {
        "Managertxt": mgr_fields.get("Title", ""),
        "SystemState": "Not Processed",
        "EmployeeID": int(employee_id),
        "ManagerID": int(manager_id),
    }
    if manager_lookup_id:
        update["ManagerLookupId"] = manager_lookup_id

    await sp_client.update_list_item_fields(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, update)
    logger.info("Assigned manager %s to CO/PO request #%s", mgr_fields.get("Title"), request_id)

    # Trigger approval pipeline
    await run_approval_pipeline(request_id)


async def run_approval_pipeline(request_id: str | int):
    """Pre-validate → send confirmation → send approval email."""
    item = await sp_client.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if not fields.get("Managertxt"):
        return
    if fields.get("SystemState") != "Not Processed":
        return

    # Set SystemState to Processing
    await sp_client.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, {"SystemState": "Processing"}
    )

    employee_id = fields.get("EmployeeID")
    manager_id = fields.get("ManagerID")
    if not employee_id or not manager_id:
        return

    employee = await get_employee_by_id(employee_id)
    manager = await get_employee_by_id(manager_id)
    if not employee or not manager:
        return

    emp_fields = employee["fields"]
    mgr_fields = manager["fields"]
    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)

    new_vacation = current_vacation - days

    # Type-specific validation
    if request_type == "Payout":
        new_payout = current_payout + days
        if new_payout > 5:
            # Payout cap auto-reject
            await sp_client.update_list_item_fields(
                settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
                {
                    "Title": "System Auto-Rejected: new Payout value will exceed 5.",
                    "Status": "Rejected",
                    "SystemState": "Processed",
                },
            )
            from app.templates_render import render_payout_cap_rejected
            html = render_payout_cap_rejected(request_id, emp_fields)
            await send_email(
                to=[emp_fields.get("EmailAddress", "")],
                subject="Payout Request - Auto Rejected",
                html_body=html,
            )
            logger.info("Auto-rejected CO/PO #%s — payout cap exceeded", request_id)
            return
        new_carryover = current_carryover
    else:
        new_carryover = current_carryover + days
        new_payout = current_payout

    # Vacation cannot go negative
    if new_vacation < 0:
        await sp_client.update_list_item_fields(
            settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
            {"Status": "Rejected", "SystemState": "Processed"},
        )
        from app.templates_render import render_system_override_reject
        html = render_system_override_reject(
            request_id, emp_fields, request_type,
            current_vacation, current_carryover, current_payout,
        )
        await send_email(
            to=[emp_fields.get("EmailAddress", "")],
            subject="Carry Over / Payout Request - Auto Rejected",
            html_body=html,
        )
        logger.info("Auto-rejected CO/PO #%s — vacation would go negative", request_id)
        return

    # Send confirmation to employee
    from app.templates_render import render_carryover_confirmation, render_payout_confirmation
    if request_type == "Carry Over":
        html = render_carryover_confirmation(
            request_id, emp_fields, days, new_vacation, new_carryover, current_payout
        )
        await send_email(
            to=[emp_fields.get("EmailAddress", "")],
            subject="Request Received for Carry Over",
            html_body=html,
        )
    else:
        html = render_payout_confirmation(
            request_id, emp_fields, days, new_vacation, current_carryover, new_payout
        )
        await send_email(
            to=[emp_fields.get("EmailAddress", "")],
            subject="Request Received for Payout",
            html_body=html,
        )

    # Send approval email to manager
    approve_url = generate_approval_url("carryover-payout", request_id, "approve", manager_id)
    reject_url = generate_approval_url("carryover-payout", request_id, "reject", manager_id)

    from app.templates_render import render_carryover_payout_approval_email
    employee_name = emp_fields.get("Title", "")
    html = render_carryover_payout_approval_email(
        request_id, request_type, employee_name, days,
        current_vacation, current_carryover, current_payout,
        new_vacation, new_carryover, new_payout,
        approve_url, reject_url,
    )
    subject = f"{request_type} Request #{request_id} Submitted by {employee_name}"
    await send_email(
        to=[mgr_fields.get("EmailAddress", ""), "mandyl@ucsh.com"],
        subject=subject,
        html_body=html,
    )
    logger.info("Sent approval email for CO/PO #%s", request_id)


async def approve_carryover_payout(request_id: str | int, manager_id: str | int) -> dict:
    """Process approval — re-validate, apply balance transfer, recalc RAD."""
    item = await sp_client.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if fields.get("SystemState") == "Processed":
        return {"error": "Already processed"}

    employee_id = fields.get("EmployeeID")
    if not employee_id:
        return {"error": "No employee ID"}

    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    async with lock_manager.lock(employee_id):
        # Re-read fresh balances
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]
        fresh_vacation = float(ef.get("CurrentVacationBalance", 0) or 0)
        fresh_carryover = float(ef.get("CarryOver", 0) or 0)
        fresh_payout = float(ef.get("Payout", 0) or 0)

        final_vacation = fresh_vacation - days

        # Re-validate at approval time
        if final_vacation < 0:
            # System override reject
            await sp_client.update_list_item_fields(
                settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
                {"Status": "Rejected", "SystemState": "Processed"},
            )
            employee_name = ef.get("Title", "")
            from app.templates_render import render_system_override_reject_at_approval
            html = render_system_override_reject_at_approval(request_id, employee_name, request_type)
            recipients = [ef.get("EmailAddress", "")]
            if mgr_fields.get("EmailAddress"):
                recipients.append(mgr_fields["EmailAddress"])
            recipients.append("mandyl@ucsh.com")
            await send_email(
                to=recipients,
                subject=f"System Override Reject: {request_type} Request #{request_id} Submitted by {employee_name}",
                html_body=html,
            )
            return {"status": "system_override_rejected"}

        # Apply balance transfer
        if request_type == "Carry Over":
            final_carryover = fresh_carryover + days
            final_payout = fresh_payout
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "CarryOver": final_carryover},
            )
        else:
            final_carryover = fresh_carryover
            final_payout = fresh_payout + days
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "Payout": final_payout},
            )

        # Recalculate Request Allow Date
        await recalculate_request_allow_date(employee_id, final_vacation, final_carryover)

    # Update request
    new_balance_str = f"{{Vacation:{final_vacation}, CarryOver:{final_carryover}, Payout:{final_payout}}}"
    await sp_client.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
        {"Status": "Approved", "SystemState": "Processed", "NewBalance": new_balance_str},
    )

    # Send approval email
    emp = await get_employee_by_id(employee_id)
    ef = emp["fields"]
    from app.templates_render import render_carryover_approved, render_payout_approved
    employee_name = ef.get("Title", "")
    balances = {
        "CurrentVacationBalance": final_vacation,
        "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": final_carryover,
        "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
        "Payout": final_payout,
    }

    if request_type == "Carry Over":
        html = render_carryover_approved(request_id, employee_name, balances)
        subject = f"Carry Over Request #{request_id} Approved"
    else:
        html = render_payout_approved(request_id, employee_name, balances)
        subject = f"Payout Request #{request_id} Approved"

    await send_email(
        to=[ef.get("EmailAddress", "")],
        cc=[mgr_fields.get("EmailAddress", "")],
        subject=subject,
        html_body=html,
        importance="High",
    )

    return {"status": "approved", "balances": balances}


async def reject_carryover_payout(request_id: str | int, manager_id: str | int) -> dict:
    """Process rejection."""
    item = await sp_client.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if fields.get("SystemState") == "Processed":
        return {"error": "Already processed"}

    employee_id = fields.get("EmployeeID")
    request_type = fields.get("TypeofRequest", "")

    await sp_client.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
        {"Status": "Rejected", "SystemState": "Processed"},
    )

    employee = await get_employee_by_id(employee_id) if employee_id else None
    emp_fields = employee["fields"] if employee else {}
    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    from app.templates_render import render_carryover_rejected, render_payout_rejected
    employee_name = emp_fields.get("Title", "")
    if request_type == "Carry Over":
        html = render_carryover_rejected(request_id, fields)
        subject = f"Carry Over Request #{request_id} Rejected"
    else:
        html = render_payout_rejected(request_id, fields)
        subject = f"Payout Request #{request_id} Rejected"

    await send_email(
        to=[emp_fields.get("EmailAddress", "")],
        cc=[mgr_fields.get("EmailAddress", "")],
        subject=subject,
        html_body=html,
    )

    return {"status": "rejected"}
