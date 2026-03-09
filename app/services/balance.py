import logging
from datetime import date

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.employee import get_employee_by_id

logger = logging.getLogger(__name__)


async def cascade_current_year(employee_id: str | int) -> dict:
    """Run Sick → Overtime → CarryOver → Vacation cascade. Returns final balances."""
    for iteration in range(60):
        emp = await get_employee_by_id(employee_id)
        fields = emp["fields"]
        sick = float(fields.get("CurrentSickDayBalance", 0) or 0)
        overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)
        carryover = float(fields.get("CarryOver", 0) or 0)
        vacation = float(fields.get("CurrentVacationBalance", 0) or 0)

        if sick < 0:
            overtime += sick
            sick = 0
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentOvertimeBalance": overtime, "CurrentSickDayBalance": sick},
            )
            continue

        if overtime < 0:
            carryover += overtime
            overtime = 0
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CarryOver": carryover, "CurrentOvertimeBalance": overtime},
            )
            continue

        if carryover < 0:
            vacation += carryover
            carryover = 0
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": vacation, "CarryOver": carryover},
            )
            continue

        # All non-negative — done
        break

    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": sick,
        "CarryOver": carryover,
        "CurrentOvertimeBalance": overtime,
    }


async def cascade_next_year(employee_id: str | int) -> dict:
    """Run Overtime → CarryOver cascade (next-year requests)."""
    for iteration in range(60):
        emp = await get_employee_by_id(employee_id)
        fields = emp["fields"]
        overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)
        carryover = float(fields.get("CarryOver", 0) or 0)

        if overtime < 0:
            carryover += overtime
            overtime = 0
            await sp_client.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CarryOver": carryover, "CurrentOvertimeBalance": overtime},
            )
            continue

        break

    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    return {
        "CurrentVacationBalance": float(fields.get("CurrentVacationBalance", 0) or 0),
        "CurrentSickDayBalance": float(fields.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": carryover,
        "CurrentOvertimeBalance": overtime,
    }


def is_next_year_request(start_date: date, end_date: date) -> bool:
    current_year = date.today().year
    return start_date.year == current_year + 1 or end_date.year == current_year + 1


async def recalculate_request_allow_date(
    employee_id: str | int, vacation: float, carryover: float
) -> None:
    today = date.today()
    end_of_next_year_march = date(today.year + 1, 3, 31)
    end_of_current_year = date(today.year, 12, 31)
    end_of_next_year = date(today.year + 1, 12, 31)

    if vacation == 0 and carryover != 0:
        new_date = end_of_next_year_march
    elif carryover == 0 and vacation != 0:
        new_date = end_of_current_year
    elif vacation == 0 and carryover == 0:
        new_date = end_of_next_year
    else:
        # Both non-zero — no change
        return

    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    current_rad = fields.get("RequestAllowDate")

    new_date_str = new_date.isoformat()
    if current_rad and current_rad[:10] == new_date_str:
        return  # No change needed

    # Include Title and Supervisor to avoid clearing them (SP PatchItem behavior)
    await sp_client.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY,
        employee_id,
        {
            "RequestAllowDate": new_date_str,
            "Title": fields.get("Title", ""),
            "Supervisor": fields.get("Supervisor", ""),
        },
    )
    logger.info("Updated RequestAllowDate for employee %s to %s", employee_id, new_date_str)


async def apply_vacation_offset(employee_id: str | int) -> None:
    """After overtime approval: if vacation negative and overtime positive, offset."""
    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    vacation = float(fields.get("CurrentVacationBalance", 0) or 0)
    overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)

    if vacation >= 0 or overtime <= 0:
        return

    new_vacation = vacation + overtime
    await sp_client.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, employee_id,
        {"CurrentVacationBalance": new_vacation, "CurrentOvertimeBalance": 0},
    )

    # Re-read — if vacation overshot positive, transfer surplus back to overtime
    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    new_vacation = float(fields.get("CurrentVacationBalance", 0) or 0)
    if new_vacation > 0:
        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentOvertimeBalance": new_vacation, "CurrentVacationBalance": 0},
        )
