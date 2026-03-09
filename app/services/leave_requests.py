import asyncio
import logging
from datetime import date, datetime

from app.config import settings
from app.graph.sharepoint import sp_client
from app.graph.email import send_email
from app.services.employee import (
    get_employee_by_name,
    get_employee_by_email,
    get_employee_by_id,
    get_manager_for_employee,
    map_location_to_province,
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
)
from app.services.concurrency import lock_manager
from app.services.approval_links import generate_approval_url
from app.services.sms import send_sms

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

    # Get employee's title from SubmittedTest
    submitter_name = fields.get("SubmittedTest", {}).get("LookupValue", "") if isinstance(fields.get("SubmittedTest"), dict) else ""
    if not submitter_name:
        submitter_name = fields.get("Title", "").split(" /// ")[0].strip()

    employee = await get_employee_by_name(submitter_name)
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

    submitter_name = fields.get("Title", "").split(" /// ")[0].strip()
    employee = await get_employee_by_name(submitter_name)
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
        "Managertxt": mgr_fields.get("Title", ""),
        "StaffLocation": emp_fields.get("Location", ""),
        "StaffDepartment": emp_fields.get("Department", ""),
    }

    # Set Manager (Person field) via LookupId
    manager_lookup_id = await _resolve_user_lookup_id(manager_email)
    if manager_lookup_id:
        update_fields["ManagerLookupId"] = manager_lookup_id

    # Set AllManagers from employee's AllManagers field
    all_managers = emp_fields.get("AllManagers")
    if all_managers:
        update_fields["AllManagers"] = all_managers

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

    submitter_name = fields.get("Title", "").split(" /// ")[0].strip()
    from app.templates_render import render_bereavement_alert
    html = render_bereavement_alert(fields, submitter_name)
    await send_email(
        to=["mandyl@ucsh.com", "generalmail@ucsh.com"],
        subject="Jury Duty / Bereavement Alert",
        html_body=html,
    )
    logger.info("Sent bereavement/jury duty alert for leave request #%s", leave_request_id)


async def send_approval_email(leave_request_id: str | int):
    """Send approval email with HMAC links to manager."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, leave_request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return
    if fields.get("Status") != "Pending":
        return
    if not fields.get("Managertxt"):
        return

    submitter_name = fields.get("Title", "").split(" /// ")[0].strip()
    employee = await get_employee_by_name(submitter_name)
    if not employee:
        return
    emp_fields = employee["fields"]

    manager = await get_employee_by_name(fields.get("Managertxt", ""))
    if not manager:
        return
    mgr_fields = manager["fields"]
    manager_id = manager["id"]

    approve_url = generate_approval_url("leave", leave_request_id, "approve", manager_id)
    reject_url = generate_approval_url("leave", leave_request_id, "reject", manager_id)

    from app.templates_render import render_leave_approval_email
    html = render_leave_approval_email(fields, emp_fields, approve_url, reject_url)

    await send_email(
        to=[mgr_fields.get("EmailAddress", "")],
        subject=f"Leave Request - {submitter_name}",
        html_body=html,
    )

    # Send SMS to manager if they have a cell number
    cell = mgr_fields.get("CellNumber", "")
    if cell:
        await send_sms(
            to=cell,
            body=f"Leave Request #{leave_request_id} for {submitter_name}. Reply \"Approve {leave_request_id}\" or \"Reject {leave_request_id}\"",
        )

    logger.info("Sent approval email for leave request #%s to %s", leave_request_id, mgr_fields.get("Title"))


async def approve_leave_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process leave approval — update SP, deduct balance, cascade, email."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return {"error": "Already processed"}
    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    submitter_name = fields.get("Title", "").split(" /// ")[0].strip()
    employee = await get_employee_by_name(submitter_name)
    if not employee:
        return {"error": "Employee not found"}
    emp_fields = employee["fields"]
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
    await send_email(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Leave Request: Approved",
        html_body=html,
    )

    # Hourly staff — no balance adjustment
    if emp_fields.get("SalaryHourly") == "Hourly":
        return {"status": "approved", "hourly": True}

    leave_type = fields.get("LeaveType", "")
    days = float(fields.get("Days", 0) or 0)

    # Bereavement / Jury Duty — no balance adjustment
    if leave_type in ("Bereavement", "Jury Duty"):
        return {"status": "approved", "no_balance_change": True}

    # Deduct balance by leave type
    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        if leave_type in ("Vacation", "Half Day or Partial Day Off"):
            new_overtime = float(ef.get("CurrentOvertimeBalance", 0) or 0) - days
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentOvertimeBalance": new_overtime},
            )
        elif leave_type == "Sick or Personal Day":
            new_sick = float(ef.get("CurrentSickDayBalance", 0) or 0) - days
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentSickDayBalance": new_sick},
            )

        # Determine cascade sequence
        start_date = _parse_date(fields.get("StartDate"))
        end_date = _parse_date(fields.get("EndDate"))
        if start_date and end_date and is_next_year_request(start_date, end_date):
            balances = await cascade_next_year(employee_id)
        else:
            balances = await cascade_current_year(employee_id)

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

    await send_email(
        to=recipients,
        subject=f"Updated Leave Balance - {submitter_name}",
        html_body=html,
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

    return {"status": "approved", "balances": balances}


async def reject_leave_request(request_id: str | int, manager_id: str | int) -> dict:
    """Process leave rejection."""
    item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("ApproveProcessedFlag") == "Processed":
        return {"error": "Already processed"}

    submitter_name = fields.get("Title", "").split(" /// ")[0].strip()
    employee = await get_employee_by_name(submitter_name)
    emp_fields = employee["fields"] if employee else {}

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    await sp_client.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id,
        {"Status": "Rejected", "ApproveProcessedFlag": "Processed"},
    )

    from app.templates_render import render_leave_rejected
    html = render_leave_rejected(fields, mgr_fields.get("Title", ""))
    await send_email(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Leave Request: Rejected",
        html_body=html,
    )

    return {"status": "rejected"}


async def _resolve_user_lookup_id(email: str) -> int | None:
    """Resolve a user email to a SP User Information List lookup ID."""
    if not email:
        return None
    try:
        from app.graph.sharepoint import sp_client
        data = await sp_client.get_list_items(
            "User Information List",
            filter=f"fields/EMail eq '{email}'",
            top=1,
        )
        if data:
            return int(data[0]["id"])
    except Exception as e:
        logger.warning("Could not resolve lookup ID for %s: %s", email, e)
    return None
