import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.dashboard_tokens import validate_dashboard_token
from app.services.employee import get_employee_by_id, ADMIN_NAMES
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

    # Leave requests
    if not type or type == "leave":
        items = await sp_client.get_list_items(
            settings.SP_LIST_LEAVE_REQUESTS,
            filter=f"fields/Title eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "leave"
            results.append(item)

    # Overtime requests
    if not type or type == "overtime":
        items = await sp_client.get_list_items(
            settings.SP_LIST_OVERTIME_REQUESTS,
            filter=f"fields/SubmitterName eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "overtime"
            results.append(item)

    # Carryover/Payout requests
    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(
            settings.SP_LIST_CARRYOVER_PAYOUT,
            filter=f"fields/SubmitterName eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "carryover-payout"
            results.append(item)

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

    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/Supervisor eq '{_esc(manager_name)}'",
    )
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

    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/Supervisor eq '{_esc(manager_name)}'",
    )
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

    pending = []

    # Leave requests pending for this manager
    items = await sp_client.get_list_items(
        settings.SP_LIST_LEAVE_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "leave", **item.get("fields", {})})

    # Overtime requests
    items = await sp_client.get_list_items(
        settings.SP_LIST_OVERTIME_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "overtime", **item.get("fields", {})})

    # Carryover/Payout requests
    items = await sp_client.get_list_items(
        settings.SP_LIST_CARRYOVER_PAYOUT,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "carryover-payout", **item.get("fields", {})})

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

    if not type or type == "leave":
        items = await sp_client.get_list_items(
            settings.SP_LIST_LEAVE_REQUESTS,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "leave"
            results.append(item)

    if not type or type == "overtime":
        items = await sp_client.get_list_items(
            settings.SP_LIST_OVERTIME_REQUESTS,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "overtime"
            results.append(item)

    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(
            settings.SP_LIST_CARRYOVER_PAYOUT,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "carryover-payout"
            results.append(item)

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

    items = await sp_client.get_list_items(
        settings.SP_LIST_LEAVE_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Approved'",
    )

    events = []
    for item in items:
        f = item.get("fields", {})
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
async def admin_balances(user: AuthUser, group_by: str | None = Query(None)):
    _require_role(user, "admin")
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
    user: AuthUser,
    type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "admin")
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
async def admin_pending(user: AuthUser):
    _require_role(user, "admin")
    pending = []

    items = await sp_client.get_list_items(
        settings.SP_LIST_LEAVE_REQUESTS,
        filter="fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "leave", **item.get("fields", {})})

    items = await sp_client.get_list_items(
        settings.SP_LIST_OVERTIME_REQUESTS,
        filter="fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "overtime", **item.get("fields", {})})

    items = await sp_client.get_list_items(
        settings.SP_LIST_CARRYOVER_PAYOUT,
        filter="fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "carryover-payout", **item.get("fields", {})})

    return {"pending": pending}


# ============================
# Admin impersonation endpoints
# ============================

@router.get("/admin/view-employee/balances")
async def admin_view_employee_balances(user: AuthUser, target_id: str = Query(...)):
    _require_role(user, "admin")
    emp = await get_employee_by_id(target_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    fields = emp["fields"]
    return {
        "employee": _format_employee(fields, target_id),
        "balances": _format_balances(fields),
    }


@router.get("/admin/view-employee/requests")
async def admin_view_employee_requests(
    user: AuthUser,
    target_id: str = Query(...),
    type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "admin")
    emp = await get_employee_by_id(target_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    emp_name = emp["fields"].get("Title", "")

    results = []

    if not type or type == "leave":
        items = await sp_client.get_list_items(
            settings.SP_LIST_LEAVE_REQUESTS,
            filter=f"fields/Title eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "leave"
            results.append(item)

    if not type or type == "overtime":
        items = await sp_client.get_list_items(
            settings.SP_LIST_OVERTIME_REQUESTS,
            filter=f"fields/SubmitterName eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "overtime"
            results.append(item)

    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(
            settings.SP_LIST_CARRYOVER_PAYOUT,
            filter=f"fields/SubmitterName eq '{_esc(emp_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "carryover-payout"
            results.append(item)

    return {"requests": results}


@router.get("/admin/view-manager/members")
async def admin_view_manager_members(user: AuthUser, target_id: str = Query(...)):
    _require_role(user, "admin")
    manager = await get_employee_by_id(target_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/Supervisor eq '{_esc(manager_name)}'",
    )
    return {"members": [
        {**_format_employee(item["fields"], item["id"]), "balances": _format_balances(item["fields"])}
        for item in items
    ]}


@router.get("/admin/view-manager/pending")
async def admin_view_manager_pending(user: AuthUser, target_id: str = Query(...)):
    _require_role(user, "admin")
    manager = await get_employee_by_id(target_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    pending = []

    items = await sp_client.get_list_items(
        settings.SP_LIST_LEAVE_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "leave", **item.get("fields", {})})

    items = await sp_client.get_list_items(
        settings.SP_LIST_OVERTIME_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "overtime", **item.get("fields", {})})

    items = await sp_client.get_list_items(
        settings.SP_LIST_CARRYOVER_PAYOUT,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Pending'",
    )
    for item in items:
        pending.append({"id": item["id"], "request_type": "carryover-payout", **item.get("fields", {})})

    return {"pending": pending}


@router.get("/admin/view-manager/requests")
async def admin_view_manager_requests(
    user: AuthUser,
    target_id: str = Query(...),
    type: str | None = Query(None),
    status: str | None = Query(None),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "admin")
    manager = await get_employee_by_id(target_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    results = []

    if not type or type == "leave":
        items = await sp_client.get_list_items(
            settings.SP_LIST_LEAVE_REQUESTS,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "leave"
            results.append(item)

    if not type or type == "overtime":
        items = await sp_client.get_list_items(
            settings.SP_LIST_OVERTIME_REQUESTS,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "overtime"
            results.append(item)

    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(
            settings.SP_LIST_CARRYOVER_PAYOUT,
            filter=f"fields/Managertxt eq '{_esc(manager_name)}'",
        )
        for item in _filter_requests(items, None, status, from_date, to_date):
            item["request_type"] = "carryover-payout"
            results.append(item)

    return {"requests": results}


@router.get("/admin/view-manager/calendar")
async def admin_view_manager_calendar(
    user: AuthUser,
    target_id: str = Query(...),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    _require_role(user, "admin")
    manager = await get_employee_by_id(target_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    manager_name = manager["fields"].get("Title", "")

    items = await sp_client.get_list_items(
        settings.SP_LIST_LEAVE_REQUESTS,
        filter=f"fields/Managertxt eq '{_esc(manager_name)}' and fields/Status eq 'Approved'",
    )

    events = []
    for item in items:
        f = item.get("fields", {})
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


@router.get("/admin/stats")
async def admin_stats(user: AuthUser):
    _require_role(user, "admin")

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


# --- Config endpoint (no auth needed, used by frontend) ---

@router.get("/config")
async def dashboard_config():
    return {"processing_enabled": settings.PROCESSING_ENABLED}


def _esc(value: str) -> str:
    return value.replace("'", "''")
