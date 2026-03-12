import logging
from datetime import date as dt_date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.dashboard_tokens import validate_dashboard_token, generate_dashboard_url
from app.services.employee import get_employee_by_id, ADMIN_NAMES
from app.services.balance import (
    simulate_leave_impact,
    simulate_overtime_impact,
    simulate_carryover_payout_impact,
    is_next_year_request,
)
from app.routes.approval import HANDLERS

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Auth dependency ---

class DashboardUser:
    def __init__(self, role: str, user_id: str):
        self.role = role
        self.user_id = user_id


async def get_dashboard_user(
    token: str = Query(...),
    role: str = Query(...),
    uid: str = Query(...),
    exp: str = Query(...),
) -> DashboardUser:
    valid, error_msg = validate_dashboard_token(role, uid, token, exp)
    if not valid:
        raise HTTPException(status_code=401, detail=error_msg)
    if role not in ("employee", "manager", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    return DashboardUser(role=role, user_id=uid)


AuthUser = Annotated[DashboardUser, Depends(get_dashboard_user)]


# --- Helper ---

def _filter_requests(items: list[dict], type_filter: str | None, status_filter: str | None,
                     from_date: str | None, to_date: str | None) -> list[dict]:
    """Client-side filtering of request items."""
    results = []
    for item in items:
        f = item.get("fields", {})
        if type_filter and f.get("LeaveType", "") != type_filter:
            continue
        if status_filter and f.get("Status", "") != status_filter:
            continue
        start = f.get("StartDate", "")
        if from_date and start and start < from_date:
            continue
        if to_date and start and start > to_date:
            continue
        results.append({"id": item.get("id"), **f})
    return results


def _format_balances(fields: dict) -> dict:
    return {
        "vacation_balance": float(fields.get("CurrentVacationBalance", 0) or 0),
        "vacation_entitlement": float(fields.get("VacationEntitlement", 0) or 0),
        "sick_balance": float(fields.get("CurrentSickDayBalance", 0) or 0),
        "sick_entitlement": float(fields.get("SickDayEntitlement", 0) or 0),
        "overtime": float(fields.get("CurrentOvertimeBalance", 0) or 0),
        "carryover": float(fields.get("CarryOver", 0) or 0),
        "payout": float(fields.get("Payout", 0) or 0),
    }


def _format_employee(fields: dict, emp_id: str | int) -> dict:
    return {
        "id": str(emp_id),
        "name": fields.get("Title", ""),
        "email": fields.get("EmailAddress", ""),
        "department": fields.get("Department", ""),
        "location": fields.get("Location", ""),
        "supervisor": fields.get("Supervisor", ""),
        "employee_type": fields.get("EmployeeType", ""),
    }


async def _build_staff_lookups() -> tuple[dict, dict]:
    """Fetch Staff Directory once, return (by_name_lower, by_id) dicts."""
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    by_name: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    for item in items:
        fields = item.get("fields", {})
        name = fields.get("Title", "")
        if name:
            by_name[name.strip().lower()] = item
        item_id = item.get("id")
        if item_id is not None:
            try:
                by_id[int(item_id)] = item
            except (ValueError, TypeError):
                pass
    return by_name, by_id


def _enrich_pending_item(
    item_data: dict, request_type: str,
    staff_by_name: dict, staff_by_id: dict,
) -> dict:
    """Add employee_name, current_balances, projected_balances, balance_unchanged."""
    staff = None
    emp_name = ""

    if request_type == "leave":
        emp_name = item_data.get("Title", "").split(" /// ")[0].strip()
        staff = staff_by_name.get(emp_name.lower()) if emp_name else None
    elif request_type == "overtime":
        emp_name = item_data.get("SubmittedByLookupValue", "")
        if not emp_name:
            sub = item_data.get("SubmittedBy", {})
            if isinstance(sub, dict):
                emp_name = sub.get("LookupValue", "")
        staff = staff_by_name.get(emp_name.lower()) if emp_name else None
    elif request_type == "carryover-payout":
        emp_id = item_data.get("EmployeeID")
        if emp_id is not None:
            try:
                staff = staff_by_id.get(int(emp_id))
            except (ValueError, TypeError):
                pass
        if staff:
            emp_name = staff["fields"].get("Title", "")

    item_data["employee_name"] = emp_name

    if not staff:
        item_data["current_balances"] = None
        item_data["projected_balances"] = None
        item_data["balance_unchanged"] = False
        return item_data

    sf = staff["fields"]
    item_data["current_balances"] = _format_balances(sf)

    # Hourly staff — no balance adjustment
    if sf.get("SalaryHourly") == "Hourly":
        item_data["projected_balances"] = None
        item_data["balance_unchanged"] = True
        return item_data

    projected_sp = None
    balance_unchanged = False

    if request_type == "leave":
        leave_type = item_data.get("LeaveType", "")
        days = float(item_data.get("Days", 0) or 0)
        is_next_year = False
        start_str = item_data.get("StartDate", "")
        end_str = item_data.get("EndDate", "")
        if start_str and end_str:
            try:
                start = dt_date.fromisoformat(start_str[:10])
                end = dt_date.fromisoformat(end_str[:10])
                is_next_year = is_next_year_request(start, end)
            except (ValueError, TypeError):
                pass
        projected_sp = simulate_leave_impact(sf, leave_type, days, is_next_year)
        balance_unchanged = projected_sp is None
    elif request_type == "overtime":
        hours = float(item_data.get("Hours", 0) or 0)
        projected_sp = simulate_overtime_impact(sf, hours)
    elif request_type == "carryover-payout":
        days = float(item_data.get("Days", 0) or 0)
        req_type = item_data.get("TypeofRequest", "")
        projected_sp = simulate_carryover_payout_impact(sf, days, req_type)

    if projected_sp:
        item_data["projected_balances"] = _format_balances({**sf, **projected_sp})
    else:
        item_data["projected_balances"] = None
    item_data["balance_unchanged"] = balance_unchanged

    return item_data


# ============================
# Employee endpoints
# ============================

@router.get("/me/balances")
async def my_balances(user: AuthUser):
    emp = await get_employee_by_id(user.user_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    fields = emp["fields"]
    return {
        "employee": _format_employee(fields, user.user_id),
        "balances": _format_balances(fields),
    }


@router.get("/me/requests")
async def my_requests(
    user: AuthUser,
    type: str | None = Query(None, description="leave, overtime, or carryover-payout"),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    emp = await get_employee_by_id(user.user_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    emp_name = emp["fields"].get("Title", "")

    results = []

    # SharePoint text fields aren't indexed — fetch all and filter client-side.

    # Leave requests — Title is "{Name} /// {notes}"
    if not type or type == "leave":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch leave requests")
            items = []
        for item in items:
            if not item.get("fields", {}).get("Title", "").startswith(emp_name):
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                r["request_type"] = "leave"
                results.append(r)

    # Overtime — SubmittedBy is Person/Group (LookupValue has display name)
    if not type or type == "overtime":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch overtime requests")
            items = []
        for item in items:
            if item.get("fields", {}).get("SubmittedByLookupValue", "") != emp_name:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                r["request_type"] = "overtime"
                results.append(r)

    # Carryover/Payout — match by EmployeeID (SD item ID)
    if not type or type == "carryover-payout":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
        except Exception:
            logger.exception("Failed to fetch carryover requests")
            items = []
        emp_id = int(user.user_id)
        for item in items:
            if item.get("fields", {}).get("EmployeeID") != emp_id:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                r["request_type"] = "carryover-payout"
                results.append(r)

    return {"requests": results}


# ============================
# Manager endpoints
# ============================

def _require_role(user: DashboardUser, *roles: str):
    if user.role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient role")


@router.get("/team/members")
async def team_members(user: AuthUser):
    _require_role(user, "manager", "admin")
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    all_staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    items = [i for i in all_staff if i.get("fields", {}).get("Supervisor", "") == manager_name]
    return {"members": [
        {**_format_employee(item["fields"], item["id"]), "balances": _format_balances(item["fields"])}
        for item in items
    ]}


@router.get("/team/balances")
async def team_balances(user: AuthUser):
    _require_role(user, "manager", "admin")
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    all_staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    items = [i for i in all_staff if i.get("fields", {}).get("Supervisor", "") == manager_name]
    return {"balances": [
        {"employee": _format_employee(item["fields"], item["id"]), "balances": _format_balances(item["fields"])}
        for item in items
    ]}


@router.get("/team/pending")
async def team_pending(user: AuthUser):
    _require_role(user, "manager", "admin")
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    staff_by_name, staff_by_id = await _build_staff_lookups()
    pending = []

    for list_id, req_type, mgr_field in [
        (settings.SP_LIST_LEAVE_REQUESTS, "leave", "Managertxt"),
        (settings.SP_LIST_OVERTIME_REQUESTS, "overtime", "ManagerLookupValue"),
        (settings.SP_LIST_CARRYOVER_PAYOUT, "carryover-payout", "Managertxt"),
    ]:
        try:
            items = await sp_client.get_list_items(list_id)
        except Exception:
            logger.exception("Failed to fetch %s items for team pending", req_type)
            continue
        for item in items:
            f = item.get("fields", {})
            if f.get("Status") != "Pending" or f.get(mgr_field, "") != manager_name:
                continue
            item_data = {"id": item["id"], "request_type": req_type, **f}
            _enrich_pending_item(item_data, req_type, staff_by_name, staff_by_id)
            pending.append(item_data)

    return {"pending": pending}


@router.get("/team/requests")
async def team_requests(
    user: AuthUser,
    type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "manager", "admin")
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    results = []

    for list_id, req_type, mgr_field in [
        (settings.SP_LIST_LEAVE_REQUESTS, "leave", "Managertxt"),
        (settings.SP_LIST_OVERTIME_REQUESTS, "overtime", "ManagerLookupValue"),
        (settings.SP_LIST_CARRYOVER_PAYOUT, "carryover-payout", "Managertxt"),
    ]:
        if type and type != req_type:
            continue
        try:
            items = await sp_client.get_list_items(list_id)
        except Exception:
            logger.exception("Failed to fetch %s items for team requests", req_type)
            continue
        for item in items:
            if item.get("fields", {}).get(mgr_field, "") != manager_name:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                r["request_type"] = req_type
                results.append(r)

    return {"requests": results}


@router.get("/team/calendar")
async def team_calendar(
    user: AuthUser,
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "manager", "admin")
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    all_leave = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)

    events = []
    for item in all_leave:
        f = item.get("fields", {})
        if f.get("Managertxt", "") != manager_name or f.get("Status") != "Approved":
            continue
        start = f.get("StartDate", "")
        end = f.get("EndDate", start)
        if from_date and start and start < from_date:
            continue
        if to_date and start and start > to_date:
            continue
        name = f.get("Title", "").split(" /// ")[0].strip()
        events.append({
            "id": item["id"],
            "employee": name,
            "start": start,
            "end": end,
            "leave_type": f.get("LeaveType", ""),
            "days": f.get("Days", ""),
        })

    return {"events": events}


@router.post("/team/approve/{request_type}/{request_id}")
async def team_approve(user: AuthUser, request_type: str, request_id: str):
    _require_role(user, "manager", "admin")
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    handler = HANDLERS.get((request_type, "approve"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")

    result = await handler(request_id, user.user_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/team/reject/{request_type}/{request_id}")
async def team_reject(user: AuthUser, request_type: str, request_id: str):
    _require_role(user, "manager", "admin")
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    handler = HANDLERS.get((request_type, "reject"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")

    result = await handler(request_id, user.user_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ============================
# Admin endpoints
# ============================

@router.get("/admin/balances")
async def admin_balances(group_by: str | None = Query(None)):
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)

    employees = []
    for item in items:
        f = item.get("fields", {})
        employees.append({
            **_format_employee(f, item["id"]),
            "balances": _format_balances(f),
        })

    if group_by in ("department", "location"):
        grouped = {}
        for emp in employees:
            key = emp.get(group_by, "Unknown")
            grouped.setdefault(key, []).append(emp)
        return {"grouped_by": group_by, "groups": grouped}

    return {"employees": employees}


@router.get("/admin/requests")
async def admin_requests(
    type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    results = []

    if not type or type == "leave":
        items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "leave"
            results.append(item)

    if not type or type == "overtime":
        items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "overtime"
            results.append(item)

    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "carryover-payout"
            results.append(item)

    return {"requests": results}


@router.get("/admin/pending")
async def admin_pending():
    staff_by_name, staff_by_id = await _build_staff_lookups()
    pending = []

    for list_id, req_type in [
        (settings.SP_LIST_LEAVE_REQUESTS, "leave"),
        (settings.SP_LIST_OVERTIME_REQUESTS, "overtime"),
        (settings.SP_LIST_CARRYOVER_PAYOUT, "carryover-payout"),
    ]:
        try:
            items = await sp_client.get_list_items(list_id)
            for item in items:
                f = item.get("fields", {})
                if f.get("Status") != "Pending":
                    continue
                item_data = {"id": item["id"], "request_type": req_type, **f}
                _enrich_pending_item(item_data, req_type, staff_by_name, staff_by_id)
                pending.append(item_data)
        except Exception:
            logger.exception("Failed to fetch pending %s items", req_type)

    return {"pending": pending}


# ============================
# Admin impersonation endpoint
# ============================

@router.get("/admin/impersonate-url")
async def admin_impersonate_url(
    target_id: str = Query(...),
    target_role: str = Query(...),
):
    if target_role not in ("employee", "manager"):
        raise HTTPException(status_code=400, detail="Role must be 'employee' or 'manager'")

    emp = await get_employee_by_id(target_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    url = generate_dashboard_url(target_role, target_id)
    return {"url": url}


@router.get("/admin/stats")
async def admin_stats():

    # Gather counts
    leave_items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    overtime_items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
    carryover_items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)

    def _count_by_status(items):
        counts = {}
        for item in items:
            status = item.get("fields", {}).get("Status", "Unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    # Department breakdown from staff directory
    staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    dept_summary = {}
    for item in staff:
        f = item.get("fields", {})
        dept = f.get("Department", "Unknown")
        if dept not in dept_summary:
            dept_summary[dept] = {"count": 0, "avg_vacation": 0, "avg_sick": 0, "total_vacation": 0, "total_sick": 0}
        dept_summary[dept]["count"] += 1
        dept_summary[dept]["total_vacation"] += float(f.get("CurrentVacationBalance", 0) or 0)
        dept_summary[dept]["total_sick"] += float(f.get("CurrentSickDayBalance", 0) or 0)

    for dept, data in dept_summary.items():
        if data["count"] > 0:
            data["avg_vacation"] = round(data["total_vacation"] / data["count"], 2)
            data["avg_sick"] = round(data["total_sick"] / data["count"], 2)

    return {
        "total_requests": {
            "leave": len(leave_items),
            "overtime": len(overtime_items),
            "carryover_payout": len(carryover_items),
        },
        "leave_by_status": _count_by_status(leave_items),
        "overtime_by_status": _count_by_status(overtime_items),
        "carryover_by_status": _count_by_status(carryover_items),
        "department_summary": dept_summary,
    }


@router.post("/admin/approve/{request_type}/{request_id}")
async def admin_approve(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    handler = HANDLERS.get((request_type, "approve"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")
    result = await handler(request_id, "0")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/admin/reject/{request_type}/{request_id}")
async def admin_reject(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    handler = HANDLERS.get((request_type, "reject"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")
    result = await handler(request_id, "0")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# --- Config endpoint (no auth needed, used by frontend) ---

@router.get("/config")
async def dashboard_config():
    return {"processing_enabled": settings.PROCESSING_ENABLED}


def _esc(value: str) -> str:
    return value.replace("'", "''")
