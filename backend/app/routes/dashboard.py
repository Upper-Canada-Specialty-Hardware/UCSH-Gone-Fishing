import logging
from datetime import date as dt_date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.dashboard_tokens import validate_dashboard_token, generate_dashboard_url
from app.services.employee import get_employee_by_id, is_manager, ADMIN_NAMES
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


def _is_in_all_managers(fields: dict, manager_name: str) -> bool:
    """Check if manager_name appears in the AllManagers Person/Group field."""
    all_managers = fields.get("AllManagers")
    if not all_managers or not isinstance(all_managers, list):
        return False
    for entry in all_managers:
        name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
        if name == manager_name:
            return True
    return False


def _is_fully_processed(fields: dict, request_type: str) -> bool:
    """Check if a pending request has been through server-side processing.

    SP-created items skip the form endpoint, so they may be missing
    calculated Days/Hours and the assigned Manager fields.
    """
    if request_type == "leave":
        days = fields.get("Days")
        if not days or float(days) == 0:
            return False
        return bool(fields.get("ManagerLookupId"))
    elif request_type == "overtime":
        hours = fields.get("Hours")
        if not hours or float(hours) == 0:
            return False
        return bool(fields.get("ManagerLookupId"))
    elif request_type == "carryover-payout":
        days = fields.get("Days")
        if not days or float(days) == 0:
            return False
        return bool(fields.get("ManagerLookupId"))
    return True


def _format_balances(fields: dict) -> dict:
    return {
        "vacation_balance": float(fields.get("CurrentVacationBalance", 0) or 0),
        "vacation_entitlement": float(fields.get("DefaultYearlyVacationDays", 0) or 0),
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
        "employee_type": fields.get("EmployeeType", ""),
    }


async def _build_staff_lookups() -> tuple[dict, dict, dict, dict]:
    """Fetch Staff Directory and User Information List.
    Returns (by_name_lower, by_id, sp_user_to_name, mgr_to_emp_names) dicts.
    """
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    by_name: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    mgr_to_emp_names: dict[str, set[str]] = {}
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

        # Build manager → employee mapping from AllManagers
        emp_name = name.strip()
        if emp_name:
            all_managers = fields.get("AllManagers")
            if all_managers and isinstance(all_managers, list):
                for entry in all_managers:
                    mgr_name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
                    if mgr_name:
                        mgr_to_emp_names.setdefault(mgr_name, set()).add(emp_name)

    # SP User Information List: map SP user IDs → display names
    sp_user_to_name: dict[int, str] = {}
    try:
        user_items = await sp_client.get_list_items("User Information List", top=5000)
        for u in user_items:
            uid = u.get("id")
            uname = u.get("fields", {}).get("Title", "")
            if uid and uname:
                sp_user_to_name[int(uid)] = uname
    except Exception:
        logger.exception("Failed to fetch User Information List for name resolution")

    return by_name, by_id, sp_user_to_name, mgr_to_emp_names


def _resolve_sp_user_name(item_data: dict, field_prefix: str, sp_user_to_name: dict) -> str:
    """Resolve a SP Person/Group lookup field to a display name."""
    lookup_id = item_data.get(f"{field_prefix}LookupId")
    if lookup_id:
        try:
            return sp_user_to_name.get(int(lookup_id), "")
        except (ValueError, TypeError):
            pass
    return ""


def _resolve_managers(staff_item: dict | None) -> str:
    """Extract manager names from a Staff Directory item."""
    if not staff_item:
        return ""
    fields = staff_item.get("fields", {})
    all_managers = fields.get("AllManagers")
    if all_managers and isinstance(all_managers, list):
        names = [
            entry.get("LookupValue", "") if isinstance(entry, dict) else ""
            for entry in all_managers
        ]
        names = [n for n in names if n]
        if names:
            return ", ".join(names)
    return ""


def _resolve_employee_name(
    item_data: dict, request_type: str,
    sp_user_to_name: dict, staff_by_id: dict,
) -> str:
    """Resolve the submitter's display name from a request item."""
    if request_type == "leave":
        return _resolve_sp_user_name(item_data, "SubmittedTest", sp_user_to_name)
    elif request_type == "overtime":
        return _resolve_sp_user_name(item_data, "SubmittedBy", sp_user_to_name)
    elif request_type == "carryover-payout":
        emp_id = item_data.get("EmployeeID")
        if emp_id is not None:
            try:
                staff = staff_by_id.get(int(emp_id))
                if staff:
                    return staff["fields"].get("Title", "")
            except (ValueError, TypeError):
                pass
    return ""


def _enrich_pending_item(
    item_data: dict, request_type: str,
    staff_by_name: dict, staff_by_id: dict, sp_user_to_name: dict,
) -> dict:
    """Add employee_name, current_balances, projected_balances, balance_unchanged."""
    emp_name = _resolve_employee_name(item_data, request_type, sp_user_to_name, staff_by_id)
    staff = None
    if request_type in ("leave", "overtime"):
        staff = staff_by_name.get(emp_name.lower()) if emp_name else None
    elif request_type == "carryover-payout":
        emp_id = item_data.get("EmployeeID")
        if emp_id is not None:
            try:
                staff = staff_by_id.get(int(emp_id))
            except (ValueError, TypeError):
                pass

    item_data["employee_name"] = emp_name
    item_data["managers"] = _resolve_managers(staff)

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
        item_data["balance_unchanged"] = "Hourly staff — no balance adjustment"
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
        balance_unchanged = f"No balance deduction for {leave_type}" if projected_sp is None else ""
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
    emp_name_lower = emp_name.strip().lower()

    managers = _resolve_managers(emp)
    _, _, sp_user_to_name, _ = await _build_staff_lookups()
    results = []

    # SharePoint text fields aren't indexed — fetch all and filter client-side.
    # Match by SubmittedTest/SubmittedBy Person/Group display name vs SD Title.

    # Leave requests — SubmittedTest Person/Group field
    if not type or type == "leave":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch leave requests")
            items = []
        for item in items:
            f = item.get("fields", {})
            submitted_name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
            if submitted_name.strip().lower() != emp_name_lower:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "leave"):
                    continue
                r["request_type"] = "leave"
                r["managers"] = managers
                results.append(r)

    # Overtime — SubmittedBy Person/Group field
    if not type or type == "overtime":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch overtime requests")
            items = []
        for item in items:
            f = item.get("fields", {})
            submitted_name = _resolve_sp_user_name(f, "SubmittedBy", sp_user_to_name)
            if submitted_name.strip().lower() != emp_name_lower:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "overtime"):
                    continue
                r["request_type"] = "overtime"
                r["managers"] = managers
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
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "carryover-payout"):
                    continue
                r["request_type"] = "carryover-payout"
                r["managers"] = managers
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
    items = [
        i for i in all_staff
        if _is_in_all_managers(i.get("fields", {}), manager_name)
    ]
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
    items = [
        i for i in all_staff
        if _is_in_all_managers(i.get("fields", {}), manager_name)
    ]
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

    staff_by_name, staff_by_id, sp_user_to_name, mgr_to_emp_names = await _build_staff_lookups()
    my_employees = mgr_to_emp_names.get(manager_name, set())
    pending = []

    # Leave — check submitter in my_employees (dynamic manager lookup)
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    except Exception:
        logger.exception("Failed to fetch leave items for team pending")
        items = []
    for item in items:
        f = item.get("fields", {})
        if f.get("Status") != "Pending":
            continue
        if not _is_fully_processed(f, "leave"):
            continue
        emp_name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
        if emp_name not in my_employees:
            continue
        item_data = {"id": item["id"], "request_type": "leave", **f}
        _enrich_pending_item(item_data, "leave", staff_by_name, staff_by_id, sp_user_to_name)
        pending.append(item_data)

    # Overtime — check Manager (via SP user ID mapping) OR submitter in my_employees
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
    except Exception:
        logger.exception("Failed to fetch overtime items for team pending")
        items = []
    for item in items:
        f = item.get("fields", {})
        if f.get("Status") != "Pending":
            continue
        if not _is_fully_processed(f, "overtime"):
            continue
        submitter_name = _resolve_sp_user_name(f, "SubmittedBy", sp_user_to_name)
        mgr_name = _resolve_sp_user_name(f, "Manager", sp_user_to_name)
        if mgr_name != manager_name and submitter_name not in my_employees:
            continue
        item_data = {"id": item["id"], "request_type": "overtime", **f}
        _enrich_pending_item(item_data, "overtime", staff_by_name, staff_by_id, sp_user_to_name)
        pending.append(item_data)

    # Carryover/Payout — check submitter in my_employees
    try:
        items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
    except Exception:
        logger.exception("Failed to fetch carryover items for team pending")
        items = []
    for item in items:
        f = item.get("fields", {})
        if f.get("Status") != "Pending":
            continue
        if not _is_fully_processed(f, "carryover-payout"):
            continue
        emp_id = f.get("EmployeeID")
        emp_name = ""
        if emp_id is not None:
            try:
                staff = staff_by_id.get(int(emp_id))
                if staff:
                    emp_name = staff["fields"].get("Title", "")
            except (ValueError, TypeError):
                pass
        if emp_name not in my_employees:
            continue
        item_data = {"id": item["id"], "request_type": "carryover-payout", **f}
        _enrich_pending_item(item_data, "carryover-payout", staff_by_name, staff_by_id, sp_user_to_name)
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

    staff_by_name, staff_by_id, sp_user_to_name, mgr_to_emp_names = await _build_staff_lookups()
    my_employees = mgr_to_emp_names.get(manager_name, set())
    results = []

    # Leave — check submitter in my_employees (dynamic manager lookup)
    if not type or type == "leave":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch leave items for team requests")
            items = []
        for item in items:
            f = item.get("fields", {})
            emp_name_check = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
            if emp_name_check not in my_employees:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "leave"):
                    continue
                r["request_type"] = "leave"
                emp_name = _resolve_employee_name(f, "leave", sp_user_to_name, staff_by_id)
                r["employee_name"] = emp_name
                r["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
                results.append(r)

    # Overtime — check Manager (via SP user ID mapping) OR submitter in my_employees
    if not type or type == "overtime":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        except Exception:
            logger.exception("Failed to fetch overtime items for team requests")
            items = []
        for item in items:
            f = item.get("fields", {})
            submitter_name = _resolve_sp_user_name(f, "SubmittedBy", sp_user_to_name)
            mgr_name = _resolve_sp_user_name(f, "Manager", sp_user_to_name)
            if mgr_name != manager_name and submitter_name not in my_employees:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "overtime"):
                    continue
                r["request_type"] = "overtime"
                emp_name = _resolve_employee_name(f, "overtime", sp_user_to_name, staff_by_id)
                r["employee_name"] = emp_name
                r["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
                results.append(r)

    # Carryover/Payout — check submitter in my_employees
    if not type or type == "carryover-payout":
        try:
            items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
        except Exception:
            logger.exception("Failed to fetch carryover items for team requests")
            items = []
        for item in items:
            f = item.get("fields", {})
            emp_id = f.get("EmployeeID")
            emp_name = ""
            if emp_id is not None:
                try:
                    staff = staff_by_id.get(int(emp_id))
                    if staff:
                        emp_name = staff["fields"].get("Title", "")
                except (ValueError, TypeError):
                    pass
            if emp_name not in my_employees:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "carryover-payout"):
                    continue
                r["request_type"] = "carryover-payout"
                emp_name = _resolve_employee_name(f, "carryover-payout", sp_user_to_name, staff_by_id)
                r["employee_name"] = emp_name
                r["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
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

    staff_by_name, staff_by_id, sp_user_to_name, mgr_to_emp_names = await _build_staff_lookups()
    my_employees = mgr_to_emp_names.get(manager_name, set())

    all_leave = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)

    events = []
    for item in all_leave:
        f = item.get("fields", {})
        if f.get("Status") != "Approved":
            continue
        emp_name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
        if emp_name not in my_employees:
            continue
        start = f.get("StartDate", "")[:10]
        end = f.get("EndDate", "")[:10] or start
        if from_date and start and start < from_date:
            continue
        if to_date and start and start > to_date:
            continue
        name = emp_name or "Unknown"
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

    # Build set of all manager names from AllManagers fields
    manager_names: set[str] = set()
    for item in items:
        f = item.get("fields", {})
        all_managers = f.get("AllManagers")
        if all_managers and isinstance(all_managers, list):
            for entry in all_managers:
                name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
                if name:
                    manager_names.add(name.strip())

    employees = []
    for item in items:
        f = item.get("fields", {})
        employees.append({
            **_format_employee(f, item["id"]),
            "balances": _format_balances(f),
            "is_manager": f.get("Title", "").strip() in manager_names,
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
    staff_by_name, staff_by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()
    results = []

    if not type or type == "leave":
        items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
        for item in _filter_requests(items, None, status, from_date, to_date):
            if item.get("Status") == "Pending" and not _is_fully_processed(item, "leave"):
                continue
            item["request_type"] = "leave"
            emp_name = _resolve_employee_name(item, "leave", sp_user_to_name, staff_by_id)
            item["employee_name"] = emp_name
            item["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
            results.append(item)

    if not type or type == "overtime":
        items = await sp_client.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
        for item in _filter_requests(items, None, status, from_date, to_date):
            if item.get("Status") == "Pending" and not _is_fully_processed(item, "overtime"):
                continue
            item["request_type"] = "overtime"
            emp_name = _resolve_employee_name(item, "overtime", sp_user_to_name, staff_by_id)
            item["employee_name"] = emp_name
            item["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
            results.append(item)

    if not type or type == "carryover-payout":
        items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
        for item in _filter_requests(items, None, status, from_date, to_date):
            if item.get("Status") == "Pending" and not _is_fully_processed(item, "carryover-payout"):
                continue
            item["request_type"] = "carryover-payout"
            emp_name = _resolve_employee_name(item, "carryover-payout", sp_user_to_name, staff_by_id)
            item["employee_name"] = emp_name
            item["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
            results.append(item)

    return {"requests": results}


@router.get("/admin/pending")
async def admin_pending():
    staff_by_name, staff_by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()
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
                if not _is_fully_processed(f, req_type):
                    continue
                item_data = {"id": item["id"], "request_type": req_type, **f}
                _enrich_pending_item(item_data, req_type, staff_by_name, staff_by_id, sp_user_to_name)
                pending.append(item_data)
        except Exception:
            logger.exception("Failed to fetch pending %s items", req_type)

    return {"pending": pending}


# ============================
# Admin stuck requests
# ============================

def _diagnose_stuck_leave(fields: dict, staff_by_name: dict, sp_user_to_name: dict) -> tuple[list[str], str]:
    """Inspect a stuck leave request and return (diagnostic_codes, detail_string)."""
    codes = []
    details = []

    # Missing dates — blocks auto_calculate_days entirely
    if not fields.get("StartDate") or not fields.get("EndDate"):
        codes.append("missing_dates")
        details.append("Start Date or End Date is missing")

    # Resolve employee from SubmittedTest person field
    emp_name = _resolve_sp_user_name(fields, "SubmittedTest", sp_user_to_name)
    staff = staff_by_name.get(emp_name.strip().lower()) if emp_name else None

    if not emp_name or not staff:
        codes.append("missing_employee")
        details.append("Cannot resolve submitter to a Staff Directory record")
    else:
        # Check AllManagers on the employee's Staff Directory record
        all_mgrs = staff.get("fields", {}).get("AllManagers")
        if not all_mgrs or not isinstance(all_mgrs, list) or len(all_mgrs) == 0:
            codes.append("missing_all_managers")
            details.append(f"Employee '{emp_name}' found but AllManagers field is empty in Staff Directory")

    # Days not calculated
    days = fields.get("Days")
    has_dates = fields.get("StartDate") and fields.get("EndDate")
    if has_dates and (not days or float(days) == 0):
        codes.append("missing_days")
        details.append("Dates present but Days not calculated")

    # Manager lookup failed (has AllManagers on SD but ManagerLookupId not set on request)
    if staff and not fields.get("ManagerLookupId"):
        all_mgrs = staff.get("fields", {}).get("AllManagers")
        if all_mgrs and isinstance(all_mgrs, list) and len(all_mgrs) > 0:
            codes.append("missing_manager_lookup")
            details.append("AllManagers populated but ManagerLookupId not set on request")

    # Approval email pending (manager assigned but email not sent)
    if fields.get("ManagerLookupId") and fields.get("ApproveProcessedFlag") != "Processed":
        codes.append("approval_email_pending")
        details.append("Manager assigned but approval email not sent")

    return codes, "; ".join(details) if details else "Unknown issue"


@router.get("/admin/stuck-requests")
async def admin_stuck_requests():
    staff_by_name, staff_by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()
    stuck = []

    try:
        items = await sp_client.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    except Exception:
        logger.exception("Failed to fetch leave requests for stuck check")
        items = []

    for item in items:
        f = item.get("fields", {})
        if f.get("Status") != "Pending":
            continue

        # Two stuck conditions:
        # 1. Not fully processed (no Days or no Manager)
        # 2. Has manager but approval email never sent
        is_stuck = (
            not _is_fully_processed(f, "leave")
            or (f.get("ManagerLookupId") and f.get("ApproveProcessedFlag") != "Processed")
        )
        if not is_stuck:
            continue

        emp_name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
        diagnostics, diagnostic_detail = _diagnose_stuck_leave(f, staff_by_name, sp_user_to_name)

        stuck.append({
            "id": item.get("id"),
            "employee_name": emp_name,
            "LeaveType": f.get("LeaveType"),
            "Title": f.get("Title"),
            "StartDate": f.get("StartDate"),
            "EndDate": f.get("EndDate"),
            "Days": f.get("Days"),
            "ManagerLookupId": f.get("ManagerLookupId"),
            "ApproveProcessedFlag": f.get("ApproveProcessedFlag"),
            "Created": f.get("Created"),
            "diagnostics": diagnostics,
            "diagnostic_detail": diagnostic_detail,
        })

    return {"stuck": stuck}


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


@router.post("/admin/send-dashboard-link/{target_id}")
async def admin_send_dashboard_link(target_id: str):
    emp = await get_employee_by_id(target_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    fields = emp["fields"]
    email = fields.get("EmailAddress", "")
    if not email:
        raise HTTPException(status_code=400, detail="Employee has no email address")
    name = fields.get("Title", "")
    if not await is_manager(name):
        raise HTTPException(status_code=400, detail="Employee is not a manager")

    dashboard_url = generate_dashboard_url("manager", target_id)

    from app.templates_render import render_dashboard_link_email
    from app.graph.email import send_email
    html = render_dashboard_link_email(manager_name=name, dashboard_url=dashboard_url)
    try:
        await send_email(to=[email], subject="Your Team Dashboard Link", html_body=html)
    except Exception as e:
        logger.exception("Failed to send dashboard link email to %s", email)
        raise HTTPException(status_code=502, detail=f"Email delivery failed: {e}")

    return {"status": "sent", "email": email}


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


ADMIN_DEFAULT_ID = "admin"


@router.post("/admin/approve/{request_type}/{request_id}")
async def admin_approve(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    handler = HANDLERS.get((request_type, "approve"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")
    result = await handler(request_id, ADMIN_DEFAULT_ID)
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
    result = await handler(request_id, ADMIN_DEFAULT_ID)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/admin/refund/{request_type}/{request_id}")
async def admin_refund(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    from app.services.leave_requests import refund_leave_request
    from app.services.overtime_requests import refund_overtime_request
    from app.services.carryover_payout import refund_carryover_payout

    refund_handlers = {
        "leave": refund_leave_request,
        "overtime": refund_overtime_request,
        "carryover-payout": refund_carryover_payout,
    }
    handler = refund_handlers.get(request_type)
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")

    result = await handler(request_id, ADMIN_DEFAULT_ID)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ============================
# Admin — Reprocess stuck requests
# ============================

class ReprocessRequest(BaseModel):
    reason: str


@router.post("/admin/reprocess/leave/{request_id}")
async def admin_reprocess_leave(request_id: str, body: ReprocessRequest):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="Reason is required")

    # Fetch current item from SharePoint
    try:
        item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    except Exception:
        logger.exception("Reprocess: failed to fetch leave request #%s", request_id)
        raise HTTPException(status_code=404, detail="Leave request not found")

    fields = item.get("fields", {})
    if fields.get("Status") != "Pending":
        raise HTTPException(
            status_code=400,
            detail=f"Request is not Pending (current status: {fields.get('Status')})",
        )

    logger.info("Admin reprocess for leave #%s — reason: %s", request_id, body.reason.strip())

    # Re-run through the dispatcher pipeline
    from app.tasks.dispatcher import dispatch_change
    try:
        await dispatch_change(settings.SP_LIST_LEAVE_REQUESTS, item)
    except Exception:
        logger.exception("Reprocess: dispatch_change failed for leave #%s", request_id)
        raise HTTPException(status_code=500, detail="Reprocessing failed — check server logs")

    # Re-fetch and diagnose remaining issues
    updated = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    uf = updated.get("fields", {})

    staff_by_name, _by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()
    remaining, detail = _diagnose_stuck_leave(uf, staff_by_name, sp_user_to_name)

    # If fully processed now, clear remaining
    if _is_fully_processed(uf, "leave") and uf.get("ApproveProcessedFlag") == "Processed":
        remaining = []
        detail = "All issues resolved"

    return {
        "status": "reprocessed",
        "request_id": request_id,
        "remaining_issues": remaining,
        "detail": f"Reprocessing complete. {detail}",
    }


# ============================
# Admin — Manager Assignments
# ============================

@router.get("/admin/manager-assignments")
async def admin_manager_assignments():
    from app.services.manager_assignments import get_all_assignments
    assignments = await get_all_assignments()
    return {"assignments": assignments}


@router.get("/admin/sp-users")
async def admin_sp_users():
    from app.services.manager_assignments import get_staff_as_sp_users
    users = await get_staff_as_sp_users()
    return {"users": users}


@router.patch("/admin/manager-assignments/{employee_id}")
async def admin_update_manager_assignment(employee_id: str, body: dict):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.manager_assignments import update_employee_managers
    manager_ids = body.get("manager_ids", [])
    result = await update_employee_managers(int(employee_id), manager_ids)
    return result


@router.post("/admin/manager-assignments/bulk")
async def admin_bulk_manager_assignment(body: dict):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.manager_assignments import preview_bulk_operation, execute_bulk_operation
    operation = body.get("operation")
    if operation not in ("replace", "add", "remove"):
        raise HTTPException(status_code=400, detail="Invalid operation")
    preview = body.get("preview", False)
    if preview:
        return await preview_bulk_operation(operation, body)
    return await execute_bulk_operation(operation, body)


# ============================
# Admin — Balance corrections
# ============================

BALANCE_FIELDS = {
    "vacation": "CurrentVacationBalance",
    "sick": "CurrentSickDayBalance",
    "overtime": "CurrentOvertimeBalance",
    "carryover": "CarryOver",
    "payout": "Payout",
}


@router.post("/admin/set-balances")
async def admin_set_balances(body: dict):
    """Set exact balance values for one or more employees.

    Body: {"corrections": [{"employee_id": "480", "vacation": 10.0, "carryover": 0}, ...]}
    Only specified balance fields are updated; others are left unchanged.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    from app.services.concurrency import lock_manager
    from app.services.balance import recalculate_request_allow_date

    corrections = body.get("corrections", [])
    if not corrections:
        raise HTTPException(status_code=400, detail="No corrections provided")

    report = {"applied": [], "errors": []}

    for entry in corrections:
        emp_id = str(entry.get("employee_id", ""))
        if not emp_id:
            report["errors"].append({"entry": entry, "error": "Missing employee_id"})
            continue

        sp_updates = {}
        for key, sp_field in BALANCE_FIELDS.items():
            if key in entry:
                sp_updates[sp_field] = float(entry[key])

        if not sp_updates:
            report["errors"].append({"employee_id": emp_id, "error": "No balance fields provided"})
            continue

        try:
            async with lock_manager.lock(emp_id):
                emp = await get_employee_by_id(emp_id)
                if not emp:
                    report["errors"].append({"employee_id": emp_id, "error": "Employee not found"})
                    continue
                fields = emp["fields"]

                before = {k: float(fields.get(sp_field, 0) or 0)
                          for k, sp_field in BALANCE_FIELDS.items()
                          if k in entry}

                await sp_client.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, emp_id, sp_updates,
                )

                vacation = sp_updates.get("CurrentVacationBalance",
                           float(fields.get("CurrentVacationBalance", 0) or 0))
                carryover = sp_updates.get("CarryOver",
                            float(fields.get("CarryOver", 0) or 0))
                await recalculate_request_allow_date(emp_id, vacation, carryover)

                report["applied"].append({
                    "employee_id": emp_id,
                    "name": fields.get("Title", ""),
                    "before": before,
                    "after": {k: float(entry[k]) for k in before},
                })
        except Exception as e:
            logger.exception("Failed to set balances for %s", emp_id)
            report["errors"].append({"employee_id": emp_id, "error": str(e)})

    return report


@router.post("/admin/clear-carryover-reset-log/{year}")
async def admin_clear_carryover_reset_log(year: int):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    from sqlalchemy import delete
    from app.database import async_session
    from app.models.carryover_reset_log import CarryoverResetLog

    async with async_session() as session:
        result = await session.execute(
            delete(CarryoverResetLog).where(CarryoverResetLog.year == year)
        )
        await session.commit()
        deleted = result.rowcount

    return {"year": year, "deleted": deleted}


# --- Config endpoint (no auth needed, used by frontend) ---

@router.get("/config")
async def dashboard_config():
    return {"processing_enabled": settings.PROCESSING_ENABLED}


def _esc(value: str) -> str:
    return value.replace("'", "''")
