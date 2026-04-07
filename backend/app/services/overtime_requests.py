import logging
from datetime import date

from app.config import settings
from app.graph.sharepoint import sp_client
from app.graph.email import send_email, send_email_with_dashboard
from app.services.sms import send_sms
from app.services.employee import (
    get_employee_by_name,
    get_employee_by_id,
    get_manager_for_employee,
    get_all_managers_for_employee,
    map_location_to_province,
    resolve_person_field,
)
from app.services.holidays import (
    get_holidays_for_province,
    get_half_friday_season,
    is_half_friday,
    is_company_holiday,
    _parse_date,
)
from app.services.balance import (
    apply_vacation_offset,
    cascade_current_year,
    recalculate_request_allow_date,
    simulate_overtime_impact,
)
from app.services.concurrency import lock_manager
from app.services.approval_links import generate_approval_url
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.audit_trail import (
    AuditTrailBuilder,
    snapshot_balances,
    describe_cascade_changes,
    write_audit_log,
)

logger = logging.getLogger(__name__)


async def process_new_overtime_request(form_data: dict, submitter_email: str) -> dict:
    """Create SP item → auto-assign manager → trigger approval pipeline."""
    hours = float(form_data.get("hours", 0))
    fields = {
        "Title": form_data.get("description", ""),
        "StartDate": form_data["date"],
        "Hours": hours,
        "Status": "Pending",
    }

    lookup_id = await _resolve_user_lookup_id(submitter_email)
    if lookup_id:
        fields["SubmittedByLookupId"] = lookup_id

        # Duplicate detection — block same-date requests
        from app.services.overlap_detection import check_overtime_overlap, OverlapError
        overlap = await check_overtime_overlap(
            submitter_lookup_id=lookup_id,
            overtime_date=form_data["date"],
        )
        if overlap:
            raise OverlapError("overtime", overlap)

    item = await sp_client.create_list_item(settings.SP_LIST_OVERTIME_REQUESTS, fields)
    item_id = item["id"]
    logger.info("Created overtime request #%s", item_id)

    # Auto-assign manager
    await auto_assign_manager(item_id, submitter_email)
    return item


async def auto_assign_manager(request_id: str | int, submitter_email: str | None = None):
    """Look up submitter → supervisor → assign manager → trigger approval."""
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    # Resolve submitter from SubmittedBy Person/Group field
    employee = await resolve_person_field(fields.get("SubmittedBy") or fields.get("SubmittedByLookupId"))
    if not employee and submitter_email:
        from app.services.employee import get_employee_by_email
        employee = await get_employee_by_email(submitter_email)
    if not employee:
        logger.warning("Cannot assign manager — employee not found for OT #%s", request_id)
        return

    managers = await get_all_managers_for_employee(employee)
    if not managers:
        return

    # Assign primary manager (first) to SP item
    manager = managers[0]
    mgr_fields = manager["fields"]
    mgr_email = mgr_fields.get("EmailAddress", "")
    manager_lookup_id = await _resolve_user_lookup_id(mgr_email)

    update = {"Status": "Pending"}
    if manager_lookup_id:
        update["ManagerLookupId"] = manager_lookup_id

    await sp_client.update_list_item_fields(settings.SP_LIST_OVERTIME_REQUESTS, request_id, update)
    logger.info("Assigned manager %s to overtime request #%s", mgr_fields.get("Title"), request_id)

    # Trigger approval pipeline with all managers
    await send_approval_email(request_id, employee, managers)


async def send_approval_email(request_id: str | int, employee: dict, managers: list[dict]):
    """Holiday check, half-friday detection, send approval email to all managers."""
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return

    emp_fields = employee["fields"]

    location = emp_fields.get("Location", "")
    province = map_location_to_province(location)
    holidays = await get_holidays_for_province(province)
    half_friday_season = get_half_friday_season(holidays)

    overtime_date = _parse_date(fields.get("StartDate"))
    if not overtime_date:
        return

    submitter_name = emp_fields.get("Title", "")

    # Holiday check — auto-reject
    is_holiday, holiday_name = is_company_holiday(overtime_date, holidays)
    if is_holiday:
        await sp_client.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id, {"Status": "Rejected"}
        )
        from app.templates_render import render_overtime_auto_rejected
        html = render_overtime_auto_rejected(fields, holiday_name)
        recipients = [emp_fields.get("EmailAddress", "")]
        for mgr in managers:
            mgr_email = mgr["fields"].get("EmailAddress", "")
            if mgr_email:
                recipients.append(mgr_email)
        await send_email(
            to=recipients,
            subject="Overtime Request - Auto Rejected",
            html_body=html,
        )
        logger.info("Auto-rejected overtime #%s — holiday: %s", request_id, holiday_name)
        return

    # Half-friday detection
    is_hf = is_half_friday(overtime_date, half_friday_season)

    from app.templates_render import render_overtime_approval_email, render_overtime_confirmation

    # Compute projected balances
    hours = float(fields.get("Hours", 0) or 0)
    projected = simulate_overtime_impact(emp_fields, hours)

    subject = f"Overtime Request - {submitter_name}"
    if is_hf:
        subject += " - Half-Day Friday Detected"

    for manager in managers:
        mgr_fields = manager["fields"]
        manager_id = manager["id"]

        approve_url = generate_approval_url("overtime", request_id, "approve", manager_id)
        reject_url = generate_approval_url("overtime", request_id, "reject", manager_id)

        html = render_overtime_approval_email(
            fields, submitter_name, approve_url, reject_url, is_hf,
            emp_fields=emp_fields, projected=projected,
        )

        await send_email_with_dashboard(
            to=[mgr_fields.get("EmailAddress", "")],
            subject=subject,
            html_body=html,
            primary_employee_id=manager_id,
        )

        # Send SMS to manager if they have a cell number
        cell = mgr_fields.get("CellNumber", "")
        if cell:
            ot_date = overtime_date.strftime("%b %d, %Y") if overtime_date else fields.get("StartDate", "")[:10]
            if projected:
                bal_line = f"If approved: MU: {projected['CurrentOvertimeBalance']}.\n"
            else:
                bal_line = ""
            await send_sms(
                to=cell,
                body=(
                    f"Time Make-Up Request #{request_id} for {submitter_name} ({hours} hrs).\n"
                    f"{ot_date}\n"
                    f"{bal_line}"
                    f"Reply \"OT Approve {request_id}\" or \"OT Reject {request_id}\""
                ),
            )

    logger.info("Sent approval email for overtime #%s to %d manager(s)", request_id, len(managers))

    # Send confirmation email to employee
    emp_email = emp_fields.get("EmailAddress", "")
    if emp_email:
        html = render_overtime_confirmation(fields, emp_fields, projected)
        await send_email_with_dashboard(
            to=[emp_email],
            subject=f"Time Make-Up Request Received - {submitter_name}",
            html_body=html,
            primary_employee_id=employee["id"],
        )


async def approve_overtime_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process overtime approval — update balance, vacation offset, recalc RAD."""
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    # Resolve employee from SubmittedBy Person/Group field
    employee = await resolve_person_field(fields.get("SubmittedBy") or fields.get("SubmittedByLookupId"))
    if not employee:
        return {"error": "Employee not found"}

    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]
    hours = float(fields.get("Hours", 0) or 0)
    days_to_add = hours / 8

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    # Hourly staff — simplified
    if emp_fields.get("SalaryHourly") == "Hourly":
        await sp_client.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {"Status": "Approved", "ApprovedDate": date.today().isoformat()},
        )
        from app.templates_render import render_overtime_hourly_approved
        html = render_overtime_hourly_approved(fields, submitter_name, mgr_fields.get("Title", ""))
        await send_email(
            to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
            subject=f"Overtime Approved - Hourly - {submitter_name}",
            html_body=html,
        )
        return {"status": "approved", "hourly": True}

    async with lock_manager.lock(employee_id):
        # Calculate new overtime balance
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("approve")
        before = snapshot_balances(ef)

        current_ot = float(ef.get("CurrentOvertimeBalance", 0) or 0)
        new_ot = current_ot + days_to_add

        # Update SD balance
        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentOvertimeBalance": new_ot},
        )
        audit.add_step(
            "Add overtime to Make-Up",
            {"CurrentOvertimeBalance": current_ot},
            {"CurrentOvertimeBalance": new_ot},
            f"Added {days_to_add} days ({hours} hours)",
        )

        # Update overtime request
        await sp_client.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {"Status": "Approved", "ApprovedDate": date.today().isoformat()},
        )

        # Vacation offset logic
        await apply_vacation_offset(employee_id)

        # Recalculate Request Allow Date
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        # Record vacation offset step if balances changed
        post_offset = snapshot_balances(ef)
        if (before["CurrentVacationBalance"] != post_offset["CurrentVacationBalance"]
                or new_ot != post_offset["CurrentOvertimeBalance"]):
            audit.add_step(
                "Vacation offset",
                {"CurrentVacationBalance": before["CurrentVacationBalance"], "CurrentOvertimeBalance": new_ot},
                {"CurrentVacationBalance": post_offset["CurrentVacationBalance"], "CurrentOvertimeBalance": post_offset["CurrentOvertimeBalance"]},
                "Offset negative vacation against positive overtime",
            )

        await recalculate_request_allow_date(
            employee_id,
            float(ef.get("CurrentVacationBalance", 0) or 0),
            float(ef.get("CarryOver", 0) or 0),
        )

    # Send approval email with updated balances
    emp = await get_employee_by_id(employee_id)
    ef = emp["fields"]
    balances = {
        "CurrentVacationBalance": float(ef.get("CurrentVacationBalance", 0) or 0),
        "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": float(ef.get("CarryOver", 0) or 0),
        "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
    }

    from app.templates_render import render_overtime_approved
    html = render_overtime_approved(fields, submitter_name, mgr_fields.get("Title", ""), balances)
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} Overtime Approved - {fields.get('StartDate', '')}",
        html_body=html,
        primary_employee_id=employee_id,
    )

    await write_audit_log(settings.SP_LIST_OVERTIME_REQUESTS, request_id, audit)

    return {"status": "approved", "balances": balances}


async def refund_overtime_request(request_id: str | int, admin_id: str | int) -> dict:
    """Reverse an approved overtime request — subtract from OT balance, cascade, recalc RAD."""
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Approved":
        return {"error": "Only approved requests can be refunded"}

    employee = await resolve_person_field(fields.get("SubmittedBy") or fields.get("SubmittedByLookupId"))
    if not employee:
        return {"error": "Employee not found"}

    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]
    hours = float(fields.get("Hours", 0) or 0)
    days_to_subtract = hours / 8

    # Update SP status
    await sp_client.update_list_item_fields(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, {"Status": "Refunded"},
    )

    # Hourly staff — no balance change
    if emp_fields.get("SalaryHourly") == "Hourly":
        from app.templates_render import render_refund_notification
        html = render_refund_notification("Overtime", request_id, submitter_name, fields, None)
        await send_email_with_dashboard(
            to=[emp_fields.get("EmailAddress", "")],
            subject=f"{submitter_name} - Overtime Request: Refunded",
            html_body=html,
            primary_employee_id=employee_id,
        )
        return {"status": "refunded", "no_balance_change": True}

    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("refund")
        before = snapshot_balances(ef)

        current_ot = float(ef.get("CurrentOvertimeBalance", 0) or 0)
        new_ot = current_ot - days_to_subtract

        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentOvertimeBalance": new_ot},
        )
        audit.add_step(
            "Refund overtime from Make-Up",
            {"CurrentOvertimeBalance": current_ot},
            {"CurrentOvertimeBalance": new_ot},
            f"Subtracted {days_to_subtract} days ({hours} hours)",
        )

        pre_cascade = {
            "CurrentSickDayBalance": before["CurrentSickDayBalance"],
            "CurrentOvertimeBalance": new_ot,
            "CarryOver": before["CarryOver"],
            "CurrentVacationBalance": before["CurrentVacationBalance"],
        }

        balances = await cascade_current_year(employee_id)

        cascade_after = {k: balances[k] for k in pre_cascade if k in balances}
        audit.add_step(
            "Cascade (current year)", pre_cascade, cascade_after,
            describe_cascade_changes(pre_cascade, cascade_after),
        )

        await recalculate_request_allow_date(
            employee_id, balances["CurrentVacationBalance"], balances["CarryOver"]
        )

    await write_audit_log(settings.SP_LIST_OVERTIME_REQUESTS, request_id, audit)

    from app.templates_render import render_refund_notification
    html = render_refund_notification("Overtime", request_id, submitter_name, fields, balances)
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Overtime Request: Refunded",
        html_body=html,
        primary_employee_id=employee_id,
    )

    return {"status": "refunded", "balances": balances}


async def reject_overtime_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process overtime rejection."""
    item = await sp_client.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    employee = await resolve_person_field(fields.get("SubmittedBy") or fields.get("SubmittedByLookupId"))
    submitter_name = employee["fields"].get("Title", "") if employee else ""
    emp_fields = employee["fields"] if employee else {}

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    await sp_client.update_list_item_fields(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, {"Status": "Rejected"}
    )

    # Read balances for rejection email
    balances = {}
    if employee:
        employee_id = employee["id"]
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]
        balances = {
            "CurrentVacationBalance": float(ef.get("CurrentVacationBalance", 0) or 0),
            "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
            "CarryOver": float(ef.get("CarryOver", 0) or 0),
            "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
        }

    from app.templates_render import render_overtime_rejected
    html = render_overtime_rejected(fields, submitter_name, mgr_fields.get("Title", ""), balances)
    emp_id = employee["id"] if employee else None
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} Overtime Rejected - {fields.get('StartDate', '')}",
        html_body=html,
        primary_employee_id=emp_id,
    )

    return {"status": "rejected"}
