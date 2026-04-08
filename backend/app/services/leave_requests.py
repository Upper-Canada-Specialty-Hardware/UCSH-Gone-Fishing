import asyncio
import logging
from datetime import date, datetime

from app.config import settings
from app.graph.sharepoint import sp_client
from app.graph.email import send_email, send_email_with_dashboard
from app.services.employee import (
    get_employee_by_name,
    get_employee_by_email,
    get_employee_by_id,
    get_manager_for_employee,
    get_all_managers_for_employee,
    map_location_to_province,
    resolve_person_field,
    resolve_person_field_name,
    ADMIN_NAMES,
)
from app.services.holidays import (
    get_holidays_for_province,
    get_half_friday_season,
    is_half_friday,
    is_company_holiday,
    _parse_date,
)
from app.services.business_days import calculate_business_days
from app.services.balance import (
    cascade_current_year,
    cascade_next_year,
    is_next_year_request,
    recalculate_request_allow_date,
    simulate_leave_impact,
)
from app.services.concurrency import lock_manager
from app.services.approval_links import generate_approval_url
from app.services.sms import send_sms
from app.services.audit_trail import (
    AuditTrailBuilder,
    snapshot_balances,
    describe_cascade_changes,
    write_audit_log,
    extract_approval_deltas,
)

logger = logging.getLogger(__name__)


async def process_new_leave_request(form_data: dict, submitter_email: str) -> dict:
    """Create SP list item + fire parallel tasks."""
    leave_type = form_data.get("leave_type", "")
    is_partial = leave_type == "Half Day or Partial Day Off"

    fields = {
        "LeaveType": leave_type,
        "Status": "Pending",
        "ApproveProcessedFlag": "Not Processed",
    }

    if is_partial:
        partial_hours = float(form_data.get("partial_hours", 0))
        fields["Days"] = partial_hours / 8
        fields["StartDate"] = form_data["start_date"]
        fields["EndDate"] = form_data["start_date"]  # Same date for partial
        fields["Title"] = form_data.get("employee_name", "")
    else:
        fields["StartDate"] = form_data["start_date"]
        fields["EndDate"] = form_data["end_date"]
        first_name = form_data.get("first_name", "")
        last_name = form_data.get("last_name", "")
        notes = form_data.get("notes", "")
        fields["Title"] = f"{first_name} {last_name} /// {notes}".strip(" /")

    # Set SubmittedTest via Claims lookup
    fields["SubmittedTestLookupId"] = await _resolve_user_lookup_id(submitter_email)

    # Duplicate detection — block overlapping date ranges
    lookup_id = fields["SubmittedTestLookupId"]
    if lookup_id:
        from app.services.overlap_detection import check_leave_overlap, OverlapError
        overlap = await check_leave_overlap(
            submitter_lookup_id=lookup_id,
            start_date=fields["StartDate"],
            end_date=fields["EndDate"],
        )
        if overlap:
            raise OverlapError("leave", overlap)

    item = await sp_client.create_list_item(settings.SP_LIST_LEAVE_REQUESTS, fields)
    item_id = item["id"]
    logger.info("Created leave request #%s", item_id)

    # Fire parallel tasks
    await asyncio.gather(
        auto_calculate_days(item_id),
        auto_assign_manager(item_id),
        send_bereavement_alert(item_id),
        return_exceptions=True,
    )

    return item


async def auto_calculate_days(leave_request_id: str | int):
    """Calculate business days or run partial-day auto-rejection checks."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, leave_request_id)
    fields = item["fields"]
    leave_type = fields.get("LeaveType", "")
    start_date = _parse_date(fields.get("StartDate"))
    end_date = _parse_date(fields.get("EndDate"))

    if not start_date or not end_date:
        logger.error("Missing dates on leave request #%s", leave_request_id)
        return

    # Resolve employee from SubmittedTest Person/Group field
    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    if not employee:
        logger.error("Cannot find employee for leave request #%s", leave_request_id)
        return

    emp_fields = employee["fields"]
    location = emp_fields.get("Location", "")
    province = map_location_to_province(location)

    holidays = await get_holidays_for_province(province)
    half_friday_season = get_half_friday_season(holidays)

    if leave_type == "Half Day or Partial Day Off":
        await _check_partial_day(leave_request_id, fields, start_date, holidays, half_friday_season, emp_fields)
        return

    # Standard leave — calculate business days
    days = calculate_business_days(start_date, end_date, holidays, half_friday_season)
    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, leave_request_id, {"Days": days}
    )
    logger.info("Calculated %s business days for leave request #%s", days, leave_request_id)


async def _check_partial_day(
    request_id, fields, start_date, holidays, half_friday_season, emp_fields
):
    """Run partial-day auto-rejection checks."""
    submitter_email = emp_fields.get("EmailAddress", "")
    days = float(fields.get("Days", 0) or 0)

    # Holiday conflict check
    holiday_match, holiday_name = is_company_holiday(start_date, holidays)
    if holiday_match:
        await sp_client.update_list_item_fields(
            settings.SP_LIST_LEAVE_REQUESTS, request_id,
            {"Status": "Rejected", "ApproveProcessedFlag": "Processed"},
        )
        if submitter_email:
            from app.templates_render import render_partial_day_holiday_rejected
            html = render_partial_day_holiday_rejected(fields, holiday_name)
            await send_email(
                to=[submitter_email],
                subject="Partial Day Request - Auto Rejected",
                html_body=html,
            )
        logger.info("Auto-rejected leave request #%s — holiday conflict: %s", request_id, holiday_name)
        return

    # Half-friday hour limit check
    if is_half_friday(start_date, half_friday_season) and days > 0.5:
        await sp_client.update_list_item_fields(
            settings.SP_LIST_LEAVE_REQUESTS, request_id,
            {"Status": "Rejected", "ApproveProcessedFlag": "Processed"},
        )
        if submitter_email:
            from app.templates_render import render_partial_day_halffriday_rejected
            html = render_partial_day_halffriday_rejected(fields)
            await send_email(
                to=[submitter_email],
                subject=f"Leave Request {request_id} {fields.get('Title', '')}",
                html_body=html,
            )
        logger.info("Auto-rejected leave request #%s — half-friday hour limit exceeded", request_id)
        return

    # Passed all checks — partial day is fine (Days already set during submission)
    logger.info("Partial day leave request #%s passed auto-rejection checks", request_id)


async def auto_assign_manager(leave_request_id: str | int):
    """Look up submitter → supervisor → patch SP item."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, leave_request_id)
    fields = item["fields"]

    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    if not employee:
        logger.warning("Cannot assign manager — employee not found for LR #%s", leave_request_id)
        return

    emp_fields = employee["fields"]
    manager = await get_manager_for_employee(employee)
    if not manager:
        logger.warning("Cannot assign manager — no supervisor for LR #%s", leave_request_id)
        return

    mgr_fields = manager["fields"]
    manager_email = mgr_fields.get("EmailAddress", "")

    update_fields = {
        "StaffLocation": emp_fields.get("Location", ""),
        "StaffDepartment": emp_fields.get("Department", ""),
    }

    # Set Manager (Person field) via LookupId
    manager_lookup_id = await _resolve_user_lookup_id(manager_email)
    if manager_lookup_id:
        update_fields["ManagerLookupId"] = manager_lookup_id

    # Set AllManagers from employee's AllManagers field (multi-value Person/Group)
    # Graph API requires LookupId array format for writing multi-value Person fields
    all_managers = emp_fields.get("AllManagers")
    if all_managers and isinstance(all_managers, list):
        lookup_ids = [
            int(entry["LookupId"]) for entry in all_managers
            if isinstance(entry, dict) and entry.get("LookupId")
        ]
        if lookup_ids:
            update_fields["AllManagersLookupId@odata.type"] = "Collection(Edm.Int32)"
            update_fields["AllManagersLookupId"] = lookup_ids

    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, leave_request_id, update_fields
    )
    logger.info("Assigned manager %s to leave request #%s", mgr_fields.get("Title"), leave_request_id)

    # Now trigger approval pipeline
    await send_approval_email(leave_request_id)


async def send_bereavement_alert(leave_request_id: str | int):
    """Send alert email if bereavement or jury duty."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, leave_request_id)
    fields = item["fields"]
    leave_type = fields.get("LeaveType", "")

    if leave_type not in ("Bereavement", "Jury Duty"):
        return

    submitter_name = await resolve_person_field_name(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    from app.templates_render import render_bereavement_alert
    html = render_bereavement_alert(fields, submitter_name)
    await send_email(
        to=["mandyl@ucsh.com", "generalmail@ucsh.com"],
        subject="Jury Duty / Bereavement Alert",
        html_body=html,
    )
    logger.info("Sent bereavement/jury duty alert for leave request #%s", leave_request_id)


async def send_approval_email(leave_request_id: str | int):
    """Send approval email with HMAC links to all managers."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, leave_request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return
    if fields.get("Status") != "Pending":
        return
    if not fields.get("ManagerLookupId"):
        return

    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    if not employee:
        return
    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")

    managers = await get_all_managers_for_employee(employee)
    if not managers:
        return

    from app.templates_render import render_leave_approval_email, render_leave_confirmation

    # Compute projected balances
    leave_type = fields.get("LeaveType", "")
    days = float(fields.get("Days", 0) or 0)
    is_next_year = False
    start_str = fields.get("StartDate", "")
    end_str = fields.get("EndDate", "")
    if start_str and end_str:
        try:
            start = _parse_date(start_str)
            end = _parse_date(end_str)
            if start and end:
                is_next_year = is_next_year_request(start, end)
        except (ValueError, TypeError):
            pass
    projected = simulate_leave_impact(emp_fields, leave_type, days, is_next_year)

    for manager in managers:
        mgr_fields = manager["fields"]
        manager_id = manager["id"]

        approve_url = generate_approval_url("leave", leave_request_id, "approve", manager_id)
        reject_url = generate_approval_url("leave", leave_request_id, "reject", manager_id)

        html = render_leave_approval_email(fields, emp_fields, approve_url, reject_url, submitter_name, projected)

        await send_email_with_dashboard(
            to=[mgr_fields.get("EmailAddress", "")],
            subject=f"Leave Request - {submitter_name}",
            html_body=html,
            primary_employee_id=manager_id,
        )

        # Send SMS to manager if they have a cell number
        cell = mgr_fields.get("CellNumber", "")
        if cell:
            if projected:
                bal_line = (
                    f"If approved: Vac: {projected['CurrentVacationBalance']}, "
                    f"Sick: {projected['CurrentSickDayBalance']}, "
                    f"MU: {projected['CurrentOvertimeBalance']}, "
                    f"CO: {projected['CarryOver']}.\n"
                )
            else:
                bal_line = "No balance change.\n"
            _s = _parse_date(start_str)
            _e = _parse_date(end_str)
            if _s and _e:
                if _s == _e:
                    date_line = f"{_s.strftime('%b %d, %Y')}\n"
                else:
                    date_line = f"{_s.strftime('%b %d')} - {_e.strftime('%b %d, %Y')}\n"
            elif start_str:
                date_line = f"{start_str[:10]}\n"
            else:
                date_line = ""
            await send_sms(
                to=cell,
                body=(
                    f"Leave Request #{leave_request_id} for {submitter_name} ({days} days {leave_type}).\n"
                    f"{date_line}"
                    f"{bal_line}"
                    f"Reply \"LR Approve {leave_request_id}\" or \"LR Reject {leave_request_id}\""
                ),
            )

        logger.info("Sent approval email for leave request #%s to %s", leave_request_id, mgr_fields.get("Title"))

    # Send confirmation email to employee
    emp_email = emp_fields.get("EmailAddress", "")
    if emp_email:
        html = render_leave_confirmation(fields, emp_fields, projected)
        await send_email_with_dashboard(
            to=[emp_email],
            subject=f"Leave Request Received - {submitter_name}",
            html_body=html,
            primary_employee_id=employee["id"],
        )


async def approve_leave_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process leave approval — update SP, deduct balance, cascade, email."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return {"error": "Already processed"}
    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    if not employee:
        return {"error": "Employee not found"}
    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    # Update SP item
    today_str = date.today().isoformat()
    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id,
        {"Status": "Approved", "ApproveProcessedFlag": "Processed", "ApprovedDate": today_str},
    )

    # Send approval confirmation email
    from app.templates_render import render_leave_approved
    html = render_leave_approved(fields, mgr_fields.get("Title", ""))
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Leave Request: Approved",
        html_body=html,
        primary_employee_id=employee_id,
    )

    leave_type = fields.get("LeaveType", "")
    days = float(fields.get("Days", 0) or 0)

    # Hourly staff — no balance adjustment (except sick leave)
    if emp_fields.get("SalaryHourly") == "Hourly" and leave_type != "Sick or Personal Day":
        return {"status": "approved", "hourly": True}

    # Bereavement / Jury Duty — no balance adjustment
    if leave_type in ("Bereavement", "Jury Duty"):
        return {"status": "approved", "no_balance_change": True}

    # Deduct balance by leave type
    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("approve")
        before = snapshot_balances(ef)

        if leave_type in ("Vacation", "Half Day or Partial Day Off"):
            new_overtime = float(ef.get("CurrentOvertimeBalance", 0) or 0) - days
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentOvertimeBalance": new_overtime},
            )
            audit.add_step(
                f"Deduct {leave_type} from Make-Up",
                {"CurrentOvertimeBalance": before["CurrentOvertimeBalance"]},
                {"CurrentOvertimeBalance": new_overtime},
                f"Deducted {days} days",
            )
        elif leave_type == "Sick or Personal Day":
            new_sick = float(ef.get("CurrentSickDayBalance", 0) or 0) - days
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentSickDayBalance": new_sick},
            )
            audit.add_step(
                "Deduct Sick/Personal from Sick",
                {"CurrentSickDayBalance": before["CurrentSickDayBalance"]},
                {"CurrentSickDayBalance": new_sick},
                f"Deducted {days} days",
            )

        # Construct pre-cascade state
        pre_cascade = {
            "CurrentSickDayBalance": before["CurrentSickDayBalance"],
            "CurrentOvertimeBalance": before["CurrentOvertimeBalance"],
            "CarryOver": before["CarryOver"],
            "CurrentVacationBalance": before["CurrentVacationBalance"],
        }
        if leave_type in ("Vacation", "Half Day or Partial Day Off"):
            pre_cascade["CurrentOvertimeBalance"] = new_overtime
        elif leave_type == "Sick or Personal Day":
            pre_cascade["CurrentSickDayBalance"] = new_sick

        # Determine cascade sequence
        start_date = _parse_date(fields.get("StartDate"))
        end_date = _parse_date(fields.get("EndDate"))
        if start_date and end_date and is_next_year_request(start_date, end_date):
            balances = await cascade_next_year(employee_id)
            cascade_label = "Cascade (next year)"
        else:
            balances = await cascade_current_year(employee_id)
            cascade_label = "Cascade (current year)"

        cascade_after = {k: balances[k] for k in pre_cascade if k in balances}
        audit.add_step(
            cascade_label, pre_cascade, cascade_after,
            describe_cascade_changes(pre_cascade, cascade_after),
        )

        # Recalculate Request Allow Date
        await recalculate_request_allow_date(
            employee_id, balances["CurrentVacationBalance"], balances["CarryOver"]
        )

    # Send balance update email
    is_next_year = start_date and end_date and is_next_year_request(start_date, end_date)
    from app.templates_render import render_leave_balance_update
    html = render_leave_balance_update(submitter_name, balances, is_next_year)

    recipients = [emp_fields.get("EmailAddress", "")]
    if mgr_fields.get("EmailAddress"):
        recipients.append(mgr_fields["EmailAddress"])

    await send_email_with_dashboard(
        to=recipients,
        subject=f"Updated Leave Balance - {submitter_name}",
        html_body=html,
        primary_employee_id=employee_id,
    )

    # Log new balances to SP item
    new_balances_str = (
        f"(Vacation:{balances['CurrentVacationBalance']})"
        f"(Sick:{balances['CurrentSickDayBalance']})"
        f"(CarryOver:{balances['CarryOver']})"
        f"(Make-Up:{balances['CurrentOvertimeBalance']})"
    )
    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id, {"NewBalances": new_balances_str}
    )

    await write_audit_log(settings.SP_LIST_LEAVE_REQUESTS, request_id, audit)

    return {"status": "approved", "balances": balances}


async def reject_leave_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process leave rejection."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return {"error": "Already processed"}

    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    submitter_name = employee["fields"].get("Title", "") if employee else ""
    emp_fields = employee["fields"] if employee else {}

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id,
        {"Status": "Rejected", "ApproveProcessedFlag": "Processed"},
    )

    from app.templates_render import render_leave_rejected
    html = render_leave_rejected(fields, mgr_fields.get("Title", ""))
    emp_id = employee["id"] if employee else None
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Leave Request: Rejected",
        html_body=html,
        primary_employee_id=emp_id,
    )

    return {"status": "rejected"}


async def refund_leave_request(request_id: str | int, admin_id: str | int) -> dict:
    """Reverse an approved leave request — restore balance, cascade, recalc RAD."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Approved":
        return {"error": "Only approved requests can be refunded"}

    employee = await resolve_person_field(fields.get("SubmittedTest") or fields.get("SubmittedTestLookupId"))
    if not employee:
        return {"error": "Employee not found"}
    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]

    leave_type = fields.get("LeaveType", "")
    days = float(fields.get("Days", 0) or 0)

    # Update SP status
    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id, {"Status": "Refunded"},
    )

    # Hourly staff (except sick leave) or bereavement/jury duty — no balance change
    is_hourly = emp_fields.get("SalaryHourly") == "Hourly"
    if leave_type in ("Bereavement", "Jury Duty") or (is_hourly and leave_type != "Sick or Personal Day"):
        from app.templates_render import render_refund_notification
        html = render_refund_notification("Leave", request_id, submitter_name, fields, None)
        await send_email_with_dashboard(
            to=[emp_fields.get("EmailAddress", "")],
            subject=f"{submitter_name} - Leave Request: Refunded",
            html_body=html,
            primary_employee_id=employee_id,
        )
        return {"status": "refunded", "no_balance_change": True}

    # Reverse the deduction
    raw_audit_log = fields.get("BalanceAuditLog", "") or ""
    approval_deltas = extract_approval_deltas(raw_audit_log)

    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("refund")
        before = snapshot_balances(ef)

        if approval_deltas is not None:
            # Precise path: reverse exact deltas from the approval audit log
            updates = {}
            for key, delta in approval_deltas.items():
                current_val = float(ef.get(key, 0) or 0)
                updates[key] = current_val - delta

            if updates:
                await sp_client.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, employee_id, updates,
                )

            audit_before = {k: float(ef.get(k, 0) or 0) for k in updates}
            audit_after = {k: updates[k] for k in updates}
            detail_parts = []
            for key in sorted(updates):
                restored = abs(updates[key] - float(ef.get(key, 0) or 0))
                if restored != 0:
                    detail_parts.append(f"{restored} restored to {key}")
            audit.add_step(
                f"Refund {leave_type} (audit-log reversal)",
                audit_before,
                audit_after,
                "; ".join(detail_parts) if detail_parts else f"Reversed {days} days",
            )

            # No cascade — audit deltas already include cascade effects
            emp_updated = await get_employee_by_id(employee_id)
            uf = emp_updated["fields"]
            balances = {
                "CurrentVacationBalance": float(uf.get("CurrentVacationBalance", 0) or 0),
                "CurrentSickDayBalance": float(uf.get("CurrentSickDayBalance", 0) or 0),
                "CurrentOvertimeBalance": float(uf.get("CurrentOvertimeBalance", 0) or 0),
                "CarryOver": float(uf.get("CarryOver", 0) or 0),
            }

        else:
            # Fallback path: naive reversal for requests without audit data
            if leave_type in ("Vacation", "Half Day or Partial Day Off"):
                new_overtime = float(ef.get("CurrentOvertimeBalance", 0) or 0) + days
                await sp_client.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                    {"CurrentOvertimeBalance": new_overtime},
                )
                audit.add_step(
                    f"Refund {leave_type} to Make-Up (fallback)",
                    {"CurrentOvertimeBalance": before["CurrentOvertimeBalance"]},
                    {"CurrentOvertimeBalance": new_overtime},
                    f"Restored {days} days (no audit log available)",
                )
            elif leave_type == "Sick or Personal Day":
                new_sick = float(ef.get("CurrentSickDayBalance", 0) or 0) + days
                await sp_client.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                    {"CurrentSickDayBalance": new_sick},
                )
                audit.add_step(
                    "Refund Sick/Personal to Sick (fallback)",
                    {"CurrentSickDayBalance": before["CurrentSickDayBalance"]},
                    {"CurrentSickDayBalance": new_sick},
                    f"Restored {days} days (no audit log available)",
                )

            pre_cascade = {
                "CurrentSickDayBalance": before["CurrentSickDayBalance"],
                "CurrentOvertimeBalance": before["CurrentOvertimeBalance"],
                "CarryOver": before["CarryOver"],
                "CurrentVacationBalance": before["CurrentVacationBalance"],
            }
            if leave_type in ("Vacation", "Half Day or Partial Day Off"):
                pre_cascade["CurrentOvertimeBalance"] = new_overtime
            elif leave_type == "Sick or Personal Day":
                pre_cascade["CurrentSickDayBalance"] = new_sick

            start_date = _parse_date(fields.get("StartDate"))
            end_date = _parse_date(fields.get("EndDate"))
            if start_date and end_date and is_next_year_request(start_date, end_date):
                balances = await cascade_next_year(employee_id)
                cascade_label = "Cascade (next year)"
            else:
                balances = await cascade_current_year(employee_id)
                cascade_label = "Cascade (current year)"

            cascade_after = {k: balances[k] for k in pre_cascade if k in balances}
            audit.add_step(
                cascade_label, pre_cascade, cascade_after,
                describe_cascade_changes(pre_cascade, cascade_after),
            )

        await recalculate_request_allow_date(
            employee_id, balances["CurrentVacationBalance"], balances.get("CarryOver", 0)
        )

    await write_audit_log(settings.SP_LIST_LEAVE_REQUESTS, request_id, audit)

    from app.templates_render import render_refund_notification
    html = render_refund_notification("Leave", request_id, submitter_name, fields, balances)
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Leave Request: Refunded",
        html_body=html,
        primary_employee_id=employee_id,
    )

    return {"status": "refunded", "balances": balances}


async def _resolve_user_lookup_id(email: str) -> int | None:
    """Resolve a user email to a SP User Information List lookup ID.

    Fetches all items and matches client-side because OData $filter on
    the User Information List fails silently via Graph API.
    """
    if not email:
        return None
    try:
        from app.graph.sharepoint import sp_client
        data = await sp_client.get_list_items("User Information List", top=5000)
        email_lower = email.lower()
        for item in data:
            item_email = item.get("fields", {}).get("EMail", "")
            if item_email and item_email.lower() == email_lower:
                return int(item["id"])
    except Exception as e:
        logger.warning("Could not resolve lookup ID for %s: %s", email, e)
    return None
