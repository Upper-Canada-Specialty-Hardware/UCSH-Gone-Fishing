import logging
from datetime import date

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.employee import get_employee_by_id

logger = logging.getLogger(__name__)


# --- Pure math functions (no SharePoint I/O) ---

def cascade_current_year_pure(
    sick: float, overtime: float, carryover: float, vacation: float,
) -> dict:
    """Sick → Overtime → CarryOver → Vacation cascade as pure math."""
    for _ in range(60):
        if sick < 0:
            overtime += sick
            sick = 0
            continue
        if overtime < 0:
            carryover += overtime
            overtime = 0
            continue
        if carryover < 0:
            vacation += carryover
            carryover = 0
            continue
        break
    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": sick,
        "CarryOver": carryover,
        "CurrentOvertimeBalance": overtime,
    }


def cascade_next_year_pure(overtime: float, carryover: float) -> dict:
    """Overtime → CarryOver cascade as pure math."""
    for _ in range(60):
        if overtime < 0:
            carryover += overtime
            overtime = 0
            continue
        break
    return {"CarryOver": carryover, "CurrentOvertimeBalance": overtime}


def apply_vacation_offset_pure(
    vacation: float, overtime: float,
) -> tuple[float, float]:
    """If vacation negative and overtime positive, offset. Returns (vacation, overtime)."""
    if vacation >= 0 or overtime <= 0:
        return vacation, overtime
    new_vacation = vacation + overtime
    new_overtime = 0.0
    if new_vacation > 0:
        new_overtime = new_vacation
        new_vacation = 0.0
    return new_vacation, new_overtime


# --- Async functions (SP reads/writes, using pure math internally) ---

async def cascade_current_year(employee_id: str | int) -> dict:
    """Run Sick → Overtime → CarryOver → Vacation cascade. Returns final balances."""
    result = {}
    for iteration in range(60):
        emp = await get_employee_by_id(employee_id)
        fields = emp["fields"]
        sick = float(fields.get("CurrentSickDayBalance", 0) or 0)
        overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)
        carryover = float(fields.get("CarryOver", 0) or 0)
        vacation = float(fields.get("CurrentVacationBalance", 0) or 0)

        result = cascade_current_year_pure(sick, overtime, carryover, vacation)

        if (result["CurrentSickDayBalance"] == sick
                and result["CurrentOvertimeBalance"] == overtime
                and result["CarryOver"] == carryover
                and result["CurrentVacationBalance"] == vacation):
            break

        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id, result,
        )

    return result


async def cascade_next_year(employee_id: str | int) -> dict:
    """Run Overtime → CarryOver cascade (next-year requests)."""
    ny_result = {}
    for iteration in range(60):
        emp = await get_employee_by_id(employee_id)
        fields = emp["fields"]
        overtime = float(fields.get("CurrentOvertimeBalance", 0) or 0)
        carryover = float(fields.get("CarryOver", 0) or 0)

        ny_result = cascade_next_year_pure(overtime, carryover)

        if (ny_result["CurrentOvertimeBalance"] == overtime
                and ny_result["CarryOver"] == carryover):
            break

        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CarryOver": ny_result["CarryOver"], "CurrentOvertimeBalance": ny_result["CurrentOvertimeBalance"]},
        )

    emp = await get_employee_by_id(employee_id)
    fields = emp["fields"]
    return {
        "CurrentVacationBalance": float(fields.get("CurrentVacationBalance", 0) or 0),
        "CurrentSickDayBalance": float(fields.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": ny_result["CarryOver"],
        "CurrentOvertimeBalance": ny_result["CurrentOvertimeBalance"],
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

    new_vacation, new_overtime = apply_vacation_offset_pure(vacation, overtime)

    if new_vacation == vacation and new_overtime == overtime:
        return

    await sp_client.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, employee_id,
        {"CurrentVacationBalance": new_vacation, "CurrentOvertimeBalance": new_overtime},
    )


# --- Simulation functions (pure math, for projected balance display) ---

def simulate_leave_impact(
    sp_fields: dict, leave_type: str, days: float, is_next_year: bool,
) -> dict | None:
    """Simulate balance impact of approving a leave request.
    Returns projected SP-style balance dict, or None for no-impact types.
    """
    if leave_type in ("Bereavement", "Jury Duty"):
        return None

    sick = float(sp_fields.get("CurrentSickDayBalance", 0) or 0)
    overtime = float(sp_fields.get("CurrentOvertimeBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)

    if leave_type in ("Vacation", "Half Day or Partial Day Off"):
        overtime -= days
    elif leave_type == "Sick or Personal Day":
        sick -= days

    if is_next_year:
        result = cascade_next_year_pure(overtime, carryover)
        overtime = result["CurrentOvertimeBalance"]
        carryover = result["CarryOver"]
    else:
        result = cascade_current_year_pure(sick, overtime, carryover, vacation)
        sick = result["CurrentSickDayBalance"]
        overtime = result["CurrentOvertimeBalance"]
        carryover = result["CarryOver"]
        vacation = result["CurrentVacationBalance"]

    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": sick,
        "CurrentOvertimeBalance": overtime,
        "CarryOver": carryover,
    }


def simulate_overtime_impact(sp_fields: dict, hours: float) -> dict:
    """Simulate balance impact of approving an overtime request."""
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)
    overtime = float(sp_fields.get("CurrentOvertimeBalance", 0) or 0)

    overtime += hours / 8
    vacation, overtime = apply_vacation_offset_pure(vacation, overtime)

    return {
        "CurrentVacationBalance": vacation,
        "CurrentSickDayBalance": float(sp_fields.get("CurrentSickDayBalance", 0) or 0),
        "CurrentOvertimeBalance": overtime,
        "CarryOver": float(sp_fields.get("CarryOver", 0) or 0),
    }


def simulate_carryover_payout_impact(
    sp_fields: dict, days: float, request_type: str,
) -> dict | None:
    """Simulate balance impact of approving a carryover/payout request.
    Returns None if vacation would go negative (system override reject).
    """
    vacation = float(sp_fields.get("CurrentVacationBalance", 0) or 0)
    carryover = float(sp_fields.get("CarryOver", 0) or 0)
    payout = float(sp_fields.get("Payout", 0) or 0)

    final_vacation = vacation - days
    if final_vacation < 0:
        return None

    if request_type == "Carry Over":
        carryover += days
    else:
        payout += days

    return {
        "CurrentVacationBalance": final_vacation,
        "CurrentSickDayBalance": float(sp_fields.get("CurrentSickDayBalance", 0) or 0),
        "CurrentOvertimeBalance": float(sp_fields.get("CurrentOvertimeBalance", 0) or 0),
        "CarryOver": carryover,
        "Payout": payout,
    }
