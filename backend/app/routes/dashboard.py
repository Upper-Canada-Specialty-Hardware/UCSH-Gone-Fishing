import logging
from datetime import date as dt_date
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.dashboard_tokens import validate_dashboard_token, generate_dashboard_url
from app.services.employee import get_employee_by_id, is_manager
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.request_submitter import SUBMITTER_PERSON_COLUMNS, submitter_email_of
from app.services.balance import (
    simulate_leave_impact,
    simulate_overtime_impact,
    simulate_carryover_payout_impact,
    is_next_year_request,
)
from app.services.employee_validation import (
    fetch_approval_email_records,
    validate_all_employee_setups,
    validate_employee_setup,
)
from app.services.overlap_detection import find_conflict_for_row
from app.services.sick_day_on_behalf import manager_may_log_sick_days
from app.services.taken_from import attach_taken_from, taken_from_for
from app.services.employment import (
    NO_COST_LEAVE,
    floor_warning,
    is_hourly,
    leave_costs_balance,
    overtime_costs_balance,
    vacation_days_counted,
)
from app.routes.approval import HANDLERS
from app.repositories.request_store import request_store, requests_in_postgres
from app.services.request_live_reads import indexed_request_items

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


def _format_balances(fields: dict, vacation_taken: float | None = None) -> dict:
    """One person's balances in the dashboard wire shape.

    Args:
        fields: Staff Directory fields (or those merged with a projection).
        vacation_taken: hourly only (#300): vacation days taken this year,
            from ``_bc_vacation_taken``; None when not worked out here.

    Returns:
        The balances dict. ``hourly`` tells the screens to show sick as the
        only balance and vacation as the ``vacation_taken`` counter.
    """
    return {
        "hourly": is_hourly(fields),                     # sick only, vacation counted
        "vacation_taken": vacation_taken,                      # the counter, or None
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
        # "Salary", "Hourly" or "Hourly BC", the type the balance engine acts
        # on (services/employment.py). No EmployeeType column exists (#205).
        "employment_type": fields.get("SalaryHourly", ""),
        # Ready / Editing gate, so the staff table can filter by it (#273).
        "system_check": fields.get("SystemCheck", ""),
    }


def _employment_type_of(staff_item: dict | None, recorded: str | None = None) -> str:
    """The employment type a request row is split by (#338).

    A request in Postgres records the type that applied when it was made, and
    that wins, so a later correction to the person (someone set as hourly by
    mistake) does not move their old requests. SharePoint requests record none,
    so they take the person's current Staff Directory value.

    Args:
        staff_item: The person's Staff Directory record, or None when unresolved.
        recorded: The type stored on the request, or None/"" when there is none.

    Returns:
        "Salary", "Hourly", "Hourly BC", or "" when the person is unknown.
    """
    if recorded:
        return recorded                                        # the request's own record wins
    if not staff_item:
        return ""                                              # unresolved person: left blank
    return staff_item.get("fields", {}).get("SalaryHourly", "") or ""


class _PersonNames(dict):
    """{SharePoint user id: display name}, plus staff names by email.

    A plain dict for every existing caller. ``by_email`` lets
    _resolve_sp_user_name name a request by its SubmitterEmail column when
    the person column is empty (submitter never visited the site).
    """

    def __init__(self):
        super().__init__()
        self.by_email: dict[str, str] = {}                     # lowercase email -> staff name


async def _build_staff_lookups() -> tuple[dict, dict, dict, dict]:
    """Fetch Staff Directory and User Information List.
    Returns (by_name_lower, by_id, sp_user_to_name, mgr_to_emp_names) dicts;
    sp_user_to_name also carries ``by_email`` (see _PersonNames).
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
    sp_user_to_name = _PersonNames()
    for item in items:                                         # staff names by email, for SubmitterEmail
        fields = item.get("fields", {})
        email = (fields.get("EmailAddress") or "").strip().lower()
        if email and fields.get("Title"):
            sp_user_to_name.by_email[email] = fields["Title"]
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
    """Resolve a SP Person/Group lookup field to a display name.

    For the submitter columns, a SubmitterEmail on the item wins: it names the
    submitter even when the person column could not be set.

    Args:
        item_data: The request item's fields.
        field_prefix: The person column, e.g. "SubmittedTest" or "Manager".
        sp_user_to_name: From _build_staff_lookups.

    Returns:
        The display name, or "" when nothing resolves.
    """
    if field_prefix in SUBMITTER_PERSON_COLUMNS:
        email = submitter_email_of(item_data)
        name = getattr(sp_user_to_name, "by_email", {}).get(email) if email else None
        if name:
            return name
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


async def _bc_vacation_taken(staff_items: list[dict]) -> dict[str, float]:
    """Vacation days each hourly person has taken this calendar year (#300).

    Their vacation is a counter, not a balance, so it is rebuilt from the
    approved requests every time (nothing stored, resets each January). Costs
    one leave list read plus the name lookups, and only when at least one of
    ``staff_items`` is hourly; a failed read leaves the counters out rather
    than failing the page.

    Args:
        staff_items: Staff Directory items (``{"id", "fields"}``) on the page.

    Returns:
        ``{lowercased name: days}`` for the hourly people among them.
    """
    bc_names = {
        (i.get("fields", {}).get("Title") or "").strip().lower()
        for i in staff_items if is_hourly(i.get("fields"))
    }
    bc_names.discard("")
    if not bc_names:                                           # nobody to count: no reads
        return {}
    try:
        _, _, sp_user_to_name, _ = await _build_staff_lookups()
        items = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    except Exception:  # noqa: BLE001 - a missing counter must not break the dashboard
        logger.exception("Could not count hourly vacation")
        return {}
    rows_by_name: dict[str, list[dict]] = {name: [] for name in bc_names}
    for item in items:
        f = item.get("fields", {})
        name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name).strip().lower()
        if name in rows_by_name:                               # only the BC people asked about
            rows_by_name[name].append(f)
    from app.services.app_settings import toronto_today       # local, like the other callers here
    year = toronto_today().year                                # the counter's calendar year
    return {name: vacation_days_counted(rows, year) for name, rows in rows_by_name.items()}


def _balances_for(item: dict, taken: dict[str, float]) -> dict:
    """``_format_balances`` for one staff item, with its counter when it has one.

    Args:
        item: A Staff Directory item.
        taken: The ``_bc_vacation_taken`` result for the page.

    Returns:
        The balances dict.
    """
    f = item.get("fields", {})
    name = (f.get("Title") or "").strip().lower()
    return _format_balances(f, taken.get(name) if is_hourly(f) else None)


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
    item_data["employment_type"] = _employment_type_of(staff)   # salaried/hourly split (#338)

    if not staff:
        item_data["current_balances"] = None
        item_data["projected_balances"] = None
        item_data["balance_unchanged"] = False
        return item_data

    sf = staff["fields"]
    item_data["current_balances"] = _format_balances(sf)
    item_data["below_zero"] = None                             # set below when approving digs under zero

    # Kinds that move nothing for this person (#300): hourly vacation is
    # counted, hourly leave other than sick and hourly overtime change nothing.
    leave_type = item_data.get("LeaveType", "")
    if request_type == "leave" and is_hourly(sf) and leave_type == "Vacation":
        item_data["projected_balances"] = None
        item_data["balance_unchanged"] = "hourly: counted as vacation taken, no balance used"
        return item_data
    if (request_type == "leave" and leave_type not in NO_COST_LEAVE and not leave_costs_balance(sf, leave_type)) or (
        request_type == "overtime" and not overtime_costs_balance(sf)
    ):
        item_data["projected_balances"] = None
        item_data["balance_unchanged"] = "Hourly staff, no balance adjustment"
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
        # Red on the card when approving takes the floor below zero (#300).
        item_data["below_zero"] = floor_warning(sf, projected_sp)
    else:
        item_data["projected_balances"] = None
    item_data["balance_unchanged"] = balance_unchanged

    return item_data


# ============================
# Employee endpoints
# ============================

@router.get("/me/balances")
async def my_balances(user: AuthUser):
    """The signed-in employee's record, balances and manager(s).

    Args:
        user: The employee, from the signed dashboard link.

    Returns:
        {"employee": {..., "managers": "A, B"}, "balances": {...}}; managers
        is "" when the Staff Directory names none.

    Raises:
        HTTPException: 404 when the employee is not in the Staff Directory.
    """
    emp = await get_employee_by_id(user.user_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    fields = emp["fields"]
    taken = await _bc_vacation_taken([emp])                    # reads only for hourly
    return {
        # Managers come from the same AllManagers column the request rows use.
        "employee": {**_format_employee(fields, user.user_id), "managers": _resolve_managers(emp)},
        "balances": _balances_for(emp, taken),
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
    results = []
    pg = await requests_in_postgres()                          # Postgres: indexed reads, no whole-list fetch
    # Only the SharePoint path needs the person-name map; skip the read in Postgres.
    sp_user_to_name = (await _build_staff_lookups())[2] if not pg else None

    async def _own_items(request_type: str, list_id: str, person_col: str) -> list[dict]:
        """This employee's rows of one kind: indexed in Postgres, name-matched in SharePoint.

        Args:
            request_type: The url-form kind.
            list_id: The SharePoint list id (SharePoint path only).
            person_col: The submitter person column to match on (SharePoint path only).

        Returns:
            The items, already narrowed to this employee.
        """
        if pg:
            return await indexed_request_items(
                request_type, employee_id=user.user_id, status=status,
                from_date=from_date, to_date=to_date,
            )
        try:
            raw = await request_store.get_list_items(list_id)  # SharePoint cannot index text, so fetch all
        except Exception:
            logger.exception("Failed to fetch %s requests", request_type)
            return []
        kept = []
        for item in raw:
            f = item.get("fields", {})
            if request_type == "carryover-payout":             # matched by EmployeeID, not a person column
                if f.get("EmployeeID") != int(user.user_id):
                    continue
            else:
                submitted = _resolve_sp_user_name(f, person_col, sp_user_to_name)
                if submitted.strip().lower() != emp_name_lower:
                    continue
            kept.append(item)
        return kept

    # Leave requests — own rows (SubmittedTest person column in SharePoint).
    if not type or type == "leave":
        for item in await _own_items("leave", settings.SP_LIST_LEAVE_REQUESTS, "SubmittedTest"):
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "leave"):
                    continue
                r["request_type"] = "leave"
                r["managers"] = managers
                results.append(r)

    # Overtime — own rows (SubmittedBy person column in SharePoint).
    if not type or type == "overtime":
        for item in await _own_items("overtime", settings.SP_LIST_OVERTIME_REQUESTS, "SubmittedBy"):
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "overtime"):
                    continue
                r["request_type"] = "overtime"
                r["managers"] = managers
                results.append(r)

    # Carryover/Payout — own rows (matched by EmployeeID in SharePoint).
    if not type or type == "carryover-payout":
        for item in await _own_items("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT, ""):
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "carryover-payout"):
                    continue
                r["request_type"] = "carryover-payout"
                r["managers"] = managers
                results.append(r)

    # Every row is this one employee, so pass their fields straight through: no
    # Staff Directory lookup is needed to project the pending ones.
    await attach_taken_from(results, employee_fields=emp["fields"])
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
    taken = await _bc_vacation_taken(items)                    # reads only when the team has hourly
    return {
        "members": [
            {**_format_employee(item["fields"], item["id"]), "balances": _balances_for(item, taken)}
            for item in items
        ],
        # Province gate for the "Log sick day" action (#130), decided on the
        # manager's own Location so the dashboard only offers it where enabled.
        "can_log_sick_days": manager_may_log_sick_days(manager["fields"]),
    }


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
    taken = await _bc_vacation_taken(items)                    # reads only when the team has hourly
    return {"balances": [
        {"employee": _format_employee(item["fields"], item["id"]), "balances": _balances_for(item, taken)}
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
        items = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
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
        items = await request_store.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
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
        items = await request_store.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
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

    # Reuse the staff map already built above to project each pending row.
    await attach_taken_from(pending, staff_by_name=staff_by_name)
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
    pg = await requests_in_postgres()                          # Postgres: indexed team reads
    # The team as Staff Directory ids, for the indexed Postgres query by employee_id.
    team_ids = {
        str(staff_by_name[nm.strip().lower()]["id"])
        for nm in my_employees
        if staff_by_name.get(nm.strip().lower(), {}).get("id") is not None
    }

    async def _team_items(request_type: str, list_id: str) -> list[dict]:
        """The team's rows of one kind: indexed in Postgres, whole-list in SharePoint.

        Args:
            request_type: The url-form kind.
            list_id: The SharePoint list id (SharePoint path only).

        Returns:
            The items; already narrowed to the team in Postgres.
        """
        if pg:
            return await indexed_request_items(
                request_type, team_employee_ids=team_ids, manager_id=str(user.user_id),
                status=status, from_date=from_date, to_date=to_date,
            )
        try:
            return await request_store.get_list_items(list_id)
        except Exception:
            logger.exception("Failed to fetch %s items for team requests", request_type)
            return []

    def _project(r: dict, request_type: str, f: dict) -> None:
        """Set a row's type, employee name and managers, the same for both paths.

        Args:
            r: The flattened request row being built.
            request_type: The url-form kind.
            f: The item's fields (carries the typed employee_name in Postgres).
        """
        r["request_type"] = request_type
        # The person blob names the employee in SharePoint; the typed column does in Postgres.
        emp_name = _resolve_employee_name(f, request_type, sp_user_to_name, staff_by_id) or f.get("employee_name", "")
        r["employee_name"] = emp_name
        r["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
        # Salaried/hourly split (#338): the request's recorded type, else the person's today.
        r["employment_type"] = _employment_type_of(
            staff_by_name.get(emp_name.lower()) if emp_name else None, f.get("employment_type"),
        )

    # Leave — the team's rows (submitter in my_employees on the SharePoint path).
    if not type or type == "leave":
        for item in await _team_items("leave", settings.SP_LIST_LEAVE_REQUESTS):
            f = item.get("fields", {})
            if not pg and _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name) not in my_employees:
                continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "leave"):
                    continue
                _project(r, "leave", f)
                results.append(r)

    # Overtime — the team's rows (Manager is me, or submitter in my_employees, on SharePoint).
    if not type or type == "overtime":
        for item in await _team_items("overtime", settings.SP_LIST_OVERTIME_REQUESTS):
            f = item.get("fields", {})
            if not pg:
                submitter_name = _resolve_sp_user_name(f, "SubmittedBy", sp_user_to_name)
                mgr_name = _resolve_sp_user_name(f, "Manager", sp_user_to_name)
                if mgr_name != manager_name and submitter_name not in my_employees:
                    continue
            for r in _filter_requests([item], None, status, from_date, to_date):
                if r.get("Status") == "Pending" and not _is_fully_processed(r, "overtime"):
                    continue
                _project(r, "overtime", f)
                results.append(r)

    # Carryover/Payout — the team's rows (employee in my_employees on the SharePoint path).
    if not type or type == "carryover-payout":
        for item in await _team_items("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT):
            f = item.get("fields", {})
            if not pg:
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
                _project(r, "carryover-payout", f)
                results.append(r)

    # Reuse the staff map already built above to compute each row's pots.
    await attach_taken_from(results, staff_by_name=staff_by_name)
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

    all_leave = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)

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

    # channel="manager_dashboard"; no link version comes through the dashboard,
    # so the stale-link check does not apply (link_version stays default None).
    result = await handler(request_id, user.user_id, "manager_dashboard")
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

    # channel="manager_dashboard" for the reject path too.
    result = await handler(request_id, user.user_id, "manager_dashboard")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/team/employees")
async def team_create_employee(user: AuthUser, body: dict):
    """Create a new Staff Directory record, assigned to the requesting manager.

    The guided, validated version of typing a new row into the Staff Directory
    list: it writes every column a request needs and refuses input that would
    not work, so the record passes the setup check the moment it exists. The new
    hire is assigned to the manager making the request as their supervisor.

    Args:
        user: The signed-in manager or admin, from the dashboard link.
        body: The submitted employee form (snake_case fields).

    Returns:
        The created record in the {"id", "fields"} shape.

    Raises:
        HTTPException: 503 when processing is off, 400 with a readable reason
            when the submission cannot become a working record.
    """
    _require_role(user, "manager", "admin")
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    # The new hire's supervisor is the manager creating them. AllManagers is a
    # Person/Group field keyed by Microsoft 365 user id, so resolve the manager's
    # record to their email and then to that id.
    manager = await get_employee_by_id(user.user_id)
    if not manager:
        raise HTTPException(status_code=404, detail="Your own staff record could not be found")
    manager_email = (manager["fields"].get("EmailAddress") or "").strip()
    manager_sp_user_id = await _resolve_user_lookup_id(manager_email)
    if not manager_sp_user_id:
        raise HTTPException(
            status_code=400,
            detail="Your own record does not resolve to a Microsoft 365 account, "
                   "so it cannot be recorded as the new hire's supervisor.",
        )

    from app.services.employee_creation import EmployeeValidationError, create_employee
    try:
        record = await create_employee(body, [manager_sp_user_id])
    except EmployeeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _after_employee_created(record)


@router.post("/team/sick-days")
async def team_log_sick_day(user: AuthUser, body: dict):
    """Record a sick day for one of the manager's employees and approve it.

    The manager-side answer to staff calling in sick without logging it: the
    manager enters the dates, the system creates the "Sick or Personal Day"
    leave request in the employee's name and approves it on the spot through
    the same handler the email link and the Approve button use, so the sick
    balance (whole sick days for hourly), any cascade and the audit log are
    handled exactly as for any other approval. Only offered where the manager's Location is in an enabled
    province (SICK_DAY_ON_BEHALF_PROVINCES) and only for their own team.

    Args:
        user: The signed-in manager or admin, from the dashboard link.
        body: ``{"employee_id", "start_date", "end_date"?, "notes"?}``.

    Returns:
        ``{"status": "approved", "request_id", "employee_name", "days",
        "balances"}``.

    Raises:
        HTTPException: 503 when processing is off, 403 when the manager's
            location is not enabled, 400 with a readable reason otherwise.
    """
    _require_role(user, "manager", "admin")
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    # Imported here, not at module load, so tests can swap the service out.
    from app.services.sick_day_on_behalf import (
        SickDayNotPermitted,
        SickDayOnBehalfError,
        log_sick_day_on_behalf,
    )
    try:
        return await log_sick_day_on_behalf(user.user_id, body)
    except SickDayNotPermitted as e:
        raise HTTPException(status_code=403, detail=str(e))
    except SickDayOnBehalfError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ============================
# Admin endpoints
# ============================

# --- Holidays editor ---------------------------------------------------------
# The app itself never writes holidays during request processing; these exist so
# HR maintains the calendar from the admin dashboard instead of the SharePoint
# list. Behind the admin password like every other /admin/* route; writes sit behind the
# PROCESSING_ENABLED gate like the other admin writes.

@router.get("/admin/holidays")
async def admin_list_holidays():
    """Every holiday row, sorted by date, for the admin grid.

    Returns:
        {"holidays": [{"id", "fields": {Title, Date, Province}}, ...]}.
    """
    from app.services.holidays import list_all_holidays
    return {"holidays": await list_all_holidays()}


@router.get("/admin/holidays/parity")
async def admin_holidays_parity():
    """Compare the SharePoint and Postgres holiday rows without writing.

    Read-only, so it runs even while processing is disabled. Lets the admin see
    whether Postgres matches SharePoint before STORAGE_HOLIDAYS is flipped.

    Returns:
        The parity report (see services.holidays.holidays_parity).
    """
    from app.services.holidays import holidays_parity
    return await holidays_parity()


@router.post("/admin/holidays/copy-to-postgres")
async def admin_copy_holidays_to_postgres():
    """Copy every Company Holiday from SharePoint into Postgres (idempotent).

    Returns:
        A summary: {"source_count", "created", "updated"}.

    Raises:
        HTTPException: 503 while processing is disabled (it writes to Postgres,
            so it is gated like the other admin writes).
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.holidays import copy_holidays_from_sharepoint
    return await copy_holidays_from_sharepoint()


@router.post("/admin/holidays")
async def admin_create_holiday(body: dict):
    """Add a holiday (or a Half Fridays START/END season marker).

    Args:
        body: {"title", "date" (YYYY-MM-DD), "province"} -- province may be blank.

    Returns:
        The created holiday in the {"id","fields"} shape.

    Raises:
        HTTPException: 503 while processing is disabled, 400 on invalid input.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.holidays import HolidayValidationError, create_holiday
    try:
        return await create_holiday(body)
    except HolidayValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/admin/holidays/{item_id}")
async def admin_update_holiday(item_id: str, body: dict):
    """Edit a holiday's name, date, or province.

    Args:
        item_id: The holiday to update.
        body: {"title", "date", "province"} -- the full replacement values.

    Returns:
        The updated holiday in the {"id","fields"} shape.

    Raises:
        HTTPException: 503 while processing is disabled, 400 on invalid input,
            404 when the holiday does not exist.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.holidays import HolidayValidationError, update_holiday
    try:
        return await update_holiday(item_id, body)
    except HolidayValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Holiday not found")


@router.delete("/admin/holidays/{item_id}")
async def admin_delete_holiday(item_id: str):
    """Remove a holiday permanently.

    Args:
        item_id: The holiday to delete.

    Returns:
        {"status": "deleted"} on success.

    Raises:
        HTTPException: 503 while processing is disabled, 404 when the holiday
            does not exist.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.holidays import delete_holiday
    try:
        await delete_holiday(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Holiday not found")
    return {"status": "deleted"}


@router.post("/admin/employees")
async def admin_create_employee(body: dict):
    """Create a new Staff Directory record from the admin dashboard.

    The same creation as the manager version (`POST /team/employees`), sharing
    the same service, with two differences that follow from where it lives:
    the admin dashboard carries no signed-in person (only the shared admin
    password, #228), and there is no implicit team — the admin
    chooses the supervisor(s), passed as `manager_ids` (SharePoint user ids from
    the /admin/sp-users picker), rather than the record being assigned to the
    caller.

    Args:
        body: The submitted employee form plus `manager_ids` — a list of the
            chosen supervisors' SharePoint user ids.

    Returns:
        The created record in the {"id", "fields"} shape.

    Raises:
        HTTPException: 503 when processing is off, 400 with a readable reason
            when the submission cannot become a working record (including no
            supervisor chosen).
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    # Coerce the chosen supervisor ids to ints, dropping anything unparseable,
    # so a stray value in the picker payload cannot break the Person-field write.
    manager_ids: list[int] = []
    for raw in body.get("manager_ids") or []:
        try:
            manager_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    from app.services.employee_creation import EmployeeValidationError, create_employee
    try:
        record = await create_employee(body, manager_ids)
    except EmployeeValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _after_employee_created(record)


async def _after_employee_created(record: dict) -> dict:
    """Follow-ups once Add Employee has written a record, on either dashboard.

    Submits any requests the person made on the request page while they were
    not on staff yet, then sends them the welcome email. Neither undoes or
    blocks the create: a failure is
    reported in the response and stays visible on the admin dashboard.

    Args:
        record: The created record from create_employee.

    Returns:
        The record plus "released": one {"held_id", "status", "detail"} per
        held request for this email, and "welcome_email": "sent", "skipped"
        (no address) or "failed".
    """
    from app.services.held_requests import release_held_requests
    email = record.get("fields", {}).get("EmailAddress", "")      # held rows are keyed by email
    try:
        results = await release_held_requests(email)
        record["released"] = [vars(r) for r in results]
    except Exception:  # noqa: BLE001 - the employee exists either way
        logger.exception("Could not release held requests for a new employee")
        record["released"] = []
        record.setdefault("notices", []).append(
            "Their waiting requests could not be submitted; see Held Requests on the admin dashboard."
        )

    # Welcome email with how to sign in and the staff tutorial (#227). After
    # the release, so anything they asked for is already on its way.
    from app.services.welcome_email import send_welcome_email
    try:
        record["welcome_email"] = "sent" if await send_welcome_email(record) else "skipped"
    except Exception:  # noqa: BLE001 - the employee exists either way
        logger.exception("Could not send the welcome email to a new employee")
        record["welcome_email"] = "failed"
        record.setdefault("notices", []).append(
            "The welcome email could not be sent. They can still sign in on the site with their email."
        )
    return record


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

    taken = await _bc_vacation_taken(items)                    # hourly vacation counters
    employees = []
    for item in items:
        f = item.get("fields", {})
        employees.append({
            **_format_employee(f, item["id"]),
            "balances": _balances_for(item, taken),
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
    pg = await requests_in_postgres()                          # Postgres: indexed read of all rows

    async def _all_items(request_type: str, list_id: str) -> list[dict]:
        """Every row of one kind: indexed in Postgres, whole-list in SharePoint.

        Args:
            request_type: The url-form kind.
            list_id: The SharePoint list id (SharePoint path only).

        Returns:
            The items of this kind.
        """
        if pg:
            return await indexed_request_items(
                request_type, status=status, from_date=from_date, to_date=to_date,
            )
        return await request_store.get_list_items(list_id)

    for request_type, list_id in (
        ("leave", settings.SP_LIST_LEAVE_REQUESTS),
        ("overtime", settings.SP_LIST_OVERTIME_REQUESTS),
        ("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT),
    ):
        if type and type != request_type:
            continue
        for item in _filter_requests(await _all_items(request_type, list_id), None, status, from_date, to_date):
            if item.get("Status") == "Pending" and not _is_fully_processed(item, request_type):
                continue
            item["request_type"] = request_type
            # The person blob names the employee in SharePoint; the typed column does in Postgres.
            emp_name = _resolve_employee_name(item, request_type, sp_user_to_name, staff_by_id) or item.get("employee_name", "")
            item["employee_name"] = emp_name
            item["managers"] = _resolve_managers(staff_by_name.get(emp_name.lower()) if emp_name else None)
            item["employment_type"] = _employment_type_of(staff_by_name.get(emp_name.lower()) if emp_name else None, item.get("employment_type"))  # recorded type wins (#338)
            results.append(item)

    # Reuse the staff map already built above; this list also feeds the staff
    # profile's request history (filtered by employee on the frontend).
    await attach_taken_from(results, staff_by_name=staff_by_name)
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
            items = await request_store.get_list_items(list_id)
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

    # Reuse the staff map already built above to project each pending row.
    await attach_taken_from(pending, staff_by_name=staff_by_name)
    return {"pending": pending}


# ============================
# Admin stuck requests
# ============================

def _diagnose_stuck_leave(
    fields: dict, staff_by_name: dict, sp_user_to_name: dict,
    blocked_by: dict | None = None, notified: bool | None = None,
) -> tuple[list[str], str]:
    """Inspect a stuck leave request and return (diagnostic_codes, detail_string).

    Args:
        fields: The request's SharePoint field values.
        staff_by_name: Staff Directory records keyed by lowercased name.
        sp_user_to_name: Microsoft 365 lookup id to display name.
        blocked_by: The approved request this one clashes with, when it has
            one. Passed in rather than looked up here, because the caller
            already holds every row needed to work it out.
        notified: Whether an approval email was ever composed for this request,
            read from the approval-state table by the caller. None when that
            record could not be read, which is reported as neither sent nor
            unsent.

    Returns:
        The diagnostic codes and a single sentence joining their details.
    """
    codes = []
    details = []

    # Blocked at approval. The manager is shown a refusal and nothing is
    # written — by design, the decision to reject is theirs — so this request
    # is otherwise indistinguishable from one nobody has opened yet. Listed
    # first because it is the reason the others cannot progress.
    if blocked_by:
        codes.append("blocked_by_overlap")
        if "day_already_booked" in blocked_by:
            details.append(
                f"Cannot be approved: {blocked_by['day_already_booked']} day is already "
                f"approved for {blocked_by['start_date']} (request #{blocked_by['item_id']}), "
                "so together they exceed one day. Reprocessing will not clear this - "
                "the manager must reject it, or the approved request must be cancelled."
            )
        else:
            details.append(
                f"Cannot be approved: overlaps request #{blocked_by['item_id']}, already "
                f"approved for {blocked_by['start_date']} to {blocked_by['end_date']}. "
                "Reprocessing will not clear this - the manager must reject it, or the "
                "approved request must be cancelled."
            )

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

    # Approval email never sent. The record of a send is the approval-state row
    # written when the email is built; nothing on the SharePoint item answers
    # this, since ApproveProcessedFlag is set only when a decision is applied,
    # which listed every request still waiting on its manager as stuck.
    if fields.get("ManagerLookupId") and notified is False:
        codes.append("approval_email_pending")
        details.append(
            "Manager assigned but the approval email was never sent - there is no "
            "record of one. Reprocess or Remind sends it"
        )

    return codes, "; ".join(details) if details else "Unknown issue"


@router.get("/admin/stuck-requests")
async def admin_stuck_requests():
    from app.services.app_settings import toronto_today

    staff_by_name, staff_by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()
    today = toronto_today()                                    # for the pending projection year
    stuck = []

    try:
        items = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    except Exception:
        logger.exception("Failed to fetch leave requests for stuck check")
        items = []

    pending_items = [i for i in items if i.get("fields", {}).get("Status") == "Pending"]

    # Which of these have had an approval email composed, read in one query for
    # the whole list. The approval-state row that the email build writes is the
    # only record of a send; ApproveProcessedFlag is set when a decision is
    # applied, so reading that listed every request awaiting a manager as stuck.
    emailed = await fetch_approval_email_records(
        [("leave", settings.SP_LIST_LEAVE_REQUESTS, item) for item in pending_items]
    )

    for item in pending_items:
        f = item.get("fields", {})

        # A request that cannot be approved because it clashes with an
        # already-approved absence. Worked out from the rows fetched above, so
        # it costs no extra SharePoint calls, and derived rather than stored so
        # it disappears by itself once the approved request is cancelled.
        blocked_by = find_conflict_for_row(items, item)

        # None when the records could not be read, which is not evidence
        # either way and so is not held against the request.
        notified = (
            None if emailed is None
            else (settings.SP_LIST_LEAVE_REQUESTS, str(item.get("id", ""))) in emailed
        )

        # Three stuck conditions:
        # 1. Not fully processed (no Days or no Manager)
        # 2. Has a manager but no record of an approval email
        # 3. Blocked at approval by an already-approved absence
        is_stuck = (
            not _is_fully_processed(f, "leave")
            or (f.get("ManagerLookupId") and notified is False)
            or blocked_by is not None
        )
        if not is_stuck:
            continue

        emp_name = _resolve_sp_user_name(f, "SubmittedTest", sp_user_to_name)
        diagnostics, diagnostic_detail = _diagnose_stuck_leave(
            f, staff_by_name, sp_user_to_name, blocked_by=blocked_by, notified=notified,
        )

        # Which pots this would take from if approved. Computed from the full SP
        # fields (f), not the trimmed row below, and from the resolved employee so
        # a stuck row still carries the same key as every other request row.
        staff = staff_by_name.get(emp_name.strip().lower()) if emp_name else None
        taken_from = taken_from_for(f, "leave", staff["fields"] if staff else None, today)

        stuck.append({
            "id": item.get("id"),
            "employee_name": emp_name,
            "employment_type": _employment_type_of(staff),    # salaried/hourly split (#338)
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
            "taken_from": taken_from,
        })

    return {"stuck": stuck}


@router.get("/admin/validate-employee/{employee_id}")
async def admin_validate_employee(employee_id: str):
    """Run the read-only employee-setup validation suite (GH #41).

    Reproduces every check a real request would exercise against the employee's
    current Staff Directory values - identity resolution, supervisor lookup,
    location/holidays, and a pure balance simulation for each leave / overtime /
    carryover-payout type - without creating a request or sending any
    notification. Read-only, so it is deliberately NOT gated on
    PROCESSING_ENABLED (safe to run in reporting-only mode).

    Behind the admin password like every other /admin/* endpoint (#228); no
    per-person role check, as the admin dashboard already shows this data.
    """
    try:
        return await validate_employee_setup(employee_id)
    except Exception:
        logger.exception("Employee validation failed for #%s", employee_id)
        raise HTTPException(status_code=500, detail="Validation failed - check server logs")


@router.get("/admin/employee-setup")
async def admin_employee_setup():
    """Run the setup checks across the WHOLE Staff Directory at once.

    Same checks as /admin/validate-employee, minus the simulations and the
    requests already in flight, applied to every record rather than one chosen
    by hand. The per-employee check is on-demand, so a broken record is only
    ever looked at after someone has already been blocked by it; this lists them
    before anyone is.

    Costs a fixed three SharePoint reads regardless of headcount. Read-only, so
    like the per-employee check it is deliberately NOT gated on
    PROCESSING_ENABLED, and like every other /admin/* endpoint it carries no
    token or role check.
    """
    try:
        return await validate_all_employee_setups()
    except Exception:
        logger.exception("Employee setup sweep failed")
        raise HTTPException(status_code=500, detail="Setup check failed - check server logs")


@router.post("/admin/employee-setup/nudge")
async def admin_employee_setup_nudge():
    """Run the setup sweep now and email the creator of every broken record.

    The same sweep the task runs each morning, triggered by hand for when a
    round of fixes has just gone in and the point is to see who is still
    outstanding. Cadence still applies: a creator already told about the same
    problems inside the last week is not told again.

    Unlike the GET above this one sends email, so it is gated on
    PROCESSING_ENABLED like every other outbound path. Like every other /admin/*
    endpoint it carries no token or role check.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    from app.tasks.setup_nudges import run_setup_nudge_sweep
    try:
        return await run_setup_nudge_sweep()
    except Exception:
        logger.exception("Setup nudge sweep failed")
        raise HTTPException(status_code=500, detail="Nudge sweep failed - check server logs")


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


@router.get("/admin/employee-dashboard-link/{target_id}")
async def admin_employee_dashboard_link(target_id: str):
    emp = await get_employee_by_id(target_id)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    url = generate_dashboard_url("employee", target_id)
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
    leave_items = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    overtime_items = await request_store.get_list_items(settings.SP_LIST_OVERTIME_REQUESTS)
    carryover_items = await request_store.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)

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


async def _run_request_action(handler, *args) -> dict:
    """Call a request action and turn a missing item into a clean 4xx (#282).

    Approve/reject/refund/remind reach a request by id, so an old email link or
    the admin table acting on a request someone has since deleted would raise a
    404 from SharePoint and surface as a 500. Catch that here, and the handler's
    own {"error": ...} result, so the caller always gets a 4xx with a reason.

    Args:
        handler: The async action (approve/reject/refund/send_reminder_now).
        *args: Its arguments.

    Returns:
        The handler's result dict.

    Raises:
        HTTPException: 404 when the request no longer exists; 400 for the
            handler's own error; 502 for any other SharePoint failure.
    """
    try:
        result = await handler(*args)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="This request no longer exists.") from None
        raise HTTPException(status_code=502, detail="The request could not be updated. Try again shortly.") from e
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/admin/approve/{request_type}/{request_id}")
async def admin_approve(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    handler = HANDLERS.get((request_type, "approve"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")
    # channel="admin_dashboard"; passed through _run_request_action to the handler.
    return await _run_request_action(handler, request_id, ADMIN_DEFAULT_ID, "admin_dashboard")


@router.post("/admin/reject/{request_type}/{request_id}")
async def admin_reject(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    handler = HANDLERS.get((request_type, "reject"))
    if not handler:
        raise HTTPException(status_code=400, detail="Invalid request type")
    # channel="admin_dashboard" for the reject path too.
    return await _run_request_action(handler, request_id, ADMIN_DEFAULT_ID, "admin_dashboard")


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

    # channel="admin_dashboard"; refund is admin-only, so this is the one source.
    return await _run_request_action(handler, request_id, ADMIN_DEFAULT_ID, "admin_dashboard")


@router.post("/admin/send-reminder/{request_type}/{request_id}")
async def admin_send_reminder(request_type: str, request_id: str):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.tasks.reminders import send_reminder_now
    return await _run_request_action(send_reminder_now, request_type, request_id)


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
        item = await request_store.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
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

    # Re-run through the dispatcher pipeline. A request that already has its
    # manager would only reach send_approval_email there, which sends once per
    # version; the admin asked for a resend, so that case asks for a new version.
    from app.tasks.dispatcher import dispatch_change
    from app.services.leave_requests import send_approval_email
    try:
        if fields.get("ManagerLookupId"):
            await send_approval_email(request_id, new_version=True)
        else:
            await dispatch_change(settings.SP_LIST_LEAVE_REQUESTS, item)
    except Exception:
        logger.exception("Reprocess: failed for leave #%s", request_id)
        raise HTTPException(status_code=500, detail="Reprocessing failed — check server logs")

    # Re-fetch and diagnose remaining issues
    updated = await request_store.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, request_id)
    uf = updated.get("fields", {})

    staff_by_name, _by_id, sp_user_to_name, _mgr_map = await _build_staff_lookups()

    # Reprocessing is what sends the approval email, so the send record is read
    # again here rather than reporting the state the request was in before it ran.
    emailed = await fetch_approval_email_records(
        [("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": request_id})]
    )
    notified = (
        None if emailed is None
        else (settings.SP_LIST_LEAVE_REQUESTS, str(request_id)) in emailed
    )
    remaining, detail = _diagnose_stuck_leave(
        uf, staff_by_name, sp_user_to_name, notified=notified,
    )

    # If fully processed now, clear remaining
    if _is_fully_processed(uf, "leave") and uf.get("ApproveProcessedFlag") == "Processed":
        remaining = []
    if not remaining:
        detail = "All issues resolved"

    return {
        "status": "reprocessed",
        "request_id": request_id,
        "remaining_issues": remaining,
        "detail": f"Reprocessing complete. {detail}",
    }


# ============================
# Admin — Edit pending requests
# ============================

class LeaveEditRequest(BaseModel):
    Days: float
    LeaveType: str
    StartDate: str
    EndDate: str
    reason: str


class OvertimeEditRequest(BaseModel):
    Hours: float
    StartDate: str
    Title: str
    reason: str


class CarryoverPayoutEditRequest(BaseModel):
    TypeofRequest: str
    Days: float
    reason: str


@router.post("/admin/edit/leave/{request_id}")
async def admin_edit_leave(request_id: str, body: LeaveEditRequest):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="Reason is required")

    from app.services.leave_requests import admin_edit_leave_request
    payload = {
        "Days": body.Days,
        "LeaveType": body.LeaveType,
        "StartDate": body.StartDate,
        "EndDate": body.EndDate,
    }
    try:
        result = await admin_edit_leave_request(request_id, payload, body.reason.strip())
    except Exception:
        logger.exception("Admin edit failed for leave #%s", request_id)
        raise HTTPException(status_code=500, detail="Edit failed — check server logs")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/admin/edit/overtime/{request_id}")
async def admin_edit_overtime(request_id: str, body: OvertimeEditRequest):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="Reason is required")

    from app.services.overtime_requests import admin_edit_overtime_request
    payload = {
        "Hours": body.Hours,
        "StartDate": body.StartDate,
        "Title": body.Title,
    }
    try:
        result = await admin_edit_overtime_request(request_id, payload, body.reason.strip())
    except Exception:
        logger.exception("Admin edit failed for overtime #%s", request_id)
        raise HTTPException(status_code=500, detail="Edit failed — check server logs")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/admin/edit/carryover-payout/{request_id}")
async def admin_edit_carryover_payout_route(request_id: str, body: CarryoverPayoutEditRequest):
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    if not body.reason.strip():
        raise HTTPException(status_code=400, detail="Reason is required")

    from app.services.carryover_payout import admin_edit_carryover_payout
    payload = {
        "TypeofRequest": body.TypeofRequest,
        "Days": body.Days,
    }
    try:
        result = await admin_edit_carryover_payout(request_id, payload, body.reason.strip())
    except Exception:
        logger.exception("Admin edit failed for CO/PO #%s", request_id)
        raise HTTPException(status_code=500, detail="Edit failed — check server logs")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ============================
# Admin — Manager Assignments
# ============================

@router.get("/admin/manager-assignments")
async def admin_manager_assignments():
    from app.services.manager_assignments import get_all_assignments
    assignments = await get_all_assignments()
    return {"assignments": assignments}


@router.get("/admin/held-requests")
async def admin_held_requests(include_closed: bool = Query(False)):
    """Requests from the request page waiting for their submitter to be added.

    Behind the admin password like every other /admin/* route (#228).

    Args:
        include_closed: Also list released and cancelled ones.

    Returns:
        {"held": [...]} newest first.
    """
    from app.services.held_requests import list_held_requests
    return {"held": await list_held_requests(include_closed)}


@router.post("/admin/held-requests/{held_id}/release")
async def admin_release_held_request(held_id: int):
    """Retry submitting one held request whose person is now on staff.

    Args:
        held_id: The held request's id.

    Returns:
        {"results": [...]}; empty when the person is still not on staff.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.held_requests import held_request_email, release_held_requests
    email = await held_request_email(held_id)
    if email is None:
        raise HTTPException(status_code=404, detail="Held request not found")
    results = await release_held_requests(email, only_id=held_id)
    if not results:
        raise HTTPException(status_code=409, detail="They are not in the Staff Directory yet. Add them first.")
    return {"results": [vars(r) for r in results]}


@router.post("/admin/held-requests/{held_id}/cancel")
async def admin_cancel_held_request(held_id: int):
    """Cancel one held request (a mistake or a duplicate).

    Args:
        held_id: The held request's id.

    Returns:
        {"cancelled": true}.
    """
    from app.services.held_requests import cancel_held_request
    if not await cancel_held_request(held_id):
        raise HTTPException(status_code=404, detail="No open held request with that id")
    return {"cancelled": True}


@router.get("/admin/request-columns")
async def admin_request_columns():
    """Report whether SubmitterEmail and RequestSource exist on the request lists.

    Behind the admin password like every other /admin/* route (#228). Read only.

    Returns:
        The report from request_submitter.ensure_request_columns.
    """
    from app.services.request_submitter import ensure_request_columns
    return await ensure_request_columns(create=False)


@router.post("/admin/request-columns")
async def admin_add_request_columns():
    """Add SubmitterEmail and RequestSource to any request list missing them.

    Needs the app's Sites.Manage.All permission. Adds only optional text
    columns, so existing items and the Microsoft Form flow are unaffected.
    Turn REQUEST_EMAIL_COLUMNS_ENABLED on once the report says ready.

    Returns:
        The report from request_submitter.ensure_request_columns.
    """
    from app.services.request_submitter import ensure_request_columns
    return await ensure_request_columns(create=True)


class CarryoverPayoutWindow(BaseModel):
    """The dates carry-over and payout requests are accepted between.

    Both None clears the window, which closes them.
    """

    start: dt_date | None = None
    end: dt_date | None = None


def _window_body(window, today) -> dict:
    """The window as the admin card reads it.

    Args:
        window: (start, end) or None.
        today: Today in Toronto.

    Returns:
        {"start", "end" (ISO or None), "open": bool, "today": ISO}.
    """
    from app.services.app_settings import window_is_open
    return {
        "start": window[0].isoformat() if window else None,
        "end": window[1].isoformat() if window else None,
        "open": window_is_open(window, today),
        "today": today.isoformat(),
    }


@router.get("/admin/carryover-payout")
async def admin_carryover_payout_window():
    """The dates carry-over and payout requests are open between (#176).

    Unauthenticated like every other /admin/* route. Read only.

    Returns:
        {"start", "end", "open", "today"}; no dates and closed by default.
    """
    from app.services.app_settings import carryover_payout_window, toronto_today
    return _window_body(await carryover_payout_window(), toronto_today())


@router.put("/admin/carryover-payout")
async def admin_set_carryover_payout_window(body: CarryoverPayoutWindow):
    """Set or clear the dates carry-over and payout requests are open (#176).

    Only new requests from the request page, held requests and /api/forms
    are affected. Requests already sent keep going through approval, and the
    Microsoft Form stays under HR's control on the bulletin board.

    Args:
        body: {"start", "end"} as YYYY-MM-DD, both inclusive; both null clears.

    Returns:
        The stored window, as GET returns it.

    Raises:
        HTTPException: 400 for one date only, or an end before the start.
    """
    from app.services.app_settings import set_carryover_payout_window, toronto_today
    try:
        window = await set_carryover_payout_window(body.start, body.end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _window_body(window, toronto_today())


@router.get("/admin/carryover-payout/export")
async def admin_export_carryover_payout():
    """Download carry-over and payout information as an Excel file (#176).

    Two sheets: every employee's vacation, carry-over and payout balances,
    and the carry-over and payout requests (only those made inside the set
    window when there is one). Unauthenticated like every other /admin/*
    route. Read only.

    Returns:
        The .xlsx file as an attachment.
    """
    from fastapi.responses import Response
    from app.services.app_settings import carryover_payout_window, toronto_today
    from app.services.carryover_export import build_workbook, export_filename
    staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)    # every employee
    requests = await request_store.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
    window, today = await carryover_payout_window(), toronto_today()
    data = build_workbook(staff, requests, window)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(today)}"'},
    )


@router.get("/admin/request-storage")
async def admin_request_storage():
    """Where the request lists live, and how many items each side holds.

    Behind the admin password like every other /admin/* route (#228). Read only.

    Returns:
        The report from request_copy.request_storage_report.
    """
    from app.services.request_copy import request_storage_report
    return await request_storage_report()


@router.post("/admin/request-storage/copy")
async def admin_copy_requests_to_postgres():
    """Copy every request from the three SharePoint lists into Postgres.

    Safe to run again while requests still live in SharePoint; refused once
    STORAGE_REQUESTS is "postgres".

    Returns:
        The summary from request_copy.copy_requests_from_sharepoint.

    Raises:
        HTTPException: 503 while processing is disabled; 409 once requests
            already live in Postgres.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    from app.services.request_copy import RequestsAlreadyMoved, copy_requests_from_sharepoint
    try:
        return await copy_requests_from_sharepoint()
    except RequestsAlreadyMoved as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    from app.repositories.staff_store import staff_store

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

                # Route through the store: on Postgres the correction lands there,
                # queues one write-back and records the balance change in the
                # history (admin_dashboard); on SharePoint it writes the list
                # directly, exactly as before.
                await staff_store.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, emp_id, sp_updates,
                    history_source="admin_dashboard",
                    history_reason="Balance correction from the admin dashboard.",
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


# ================================
# Admin: person panel (view and edit)
# ================================

class EntitlementChanges(BaseModel):
    """Body for the entitlements PATCH: the new values and the reason for them.

    Attributes:
        changes: snake_case wire key -> new numeric value (see
            ``staff_entitlements.EDITABLE`` for the editable balance pots).
        fields: snake_case wire key -> new value for the Staff Directory text,
            choice and person fields (see ``staff_entitlements.TEXT_FIELDS`` /
            ``PERSON_FIELDS`` and the managers key). Empty when only pots change.
        reason: Why HR made the change; required and stored with the history.
    """
    # ``Any`` not ``float``: Pydantic v2 would coerce a stray bool or digit
    # string into a float, hiding a bad value. The service's ``_as_number``
    # does the real type-checking and rejects non-numbers with a clear message.
    changes: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    reason: str


@router.get("/admin/employees/{employee_id}")
async def admin_get_employee(employee_id: str):
    """Read one person's record for the admin person panel.

    Used by the admin person panel to show the editable entitlements and
    balances, the resolved people fields, and the change history. Behind the
    admin password like every ``/admin/`` route.

    Args:
        employee_id: Staff Directory item id of the person to show.

    Returns:
        The staff record payload (fields, people, history).

    Raises:
        HTTPException: 404 when no such employee exists.
    """
    from app.services.staff_entitlements import get_staff_record

    record = await get_staff_record(employee_id)      # panel payload or None
    if record is None:                                # unknown id
        raise HTTPException(status_code=404, detail="Employee not found")
    return record


@router.patch("/admin/employees/{employee_id}/entitlements")
async def admin_update_entitlements(employee_id: str, body: EntitlementChanges):
    """Change a person's entitlements and balances, with a recorded reason.

    Used by the admin person panel's save action. Writes only the figures that
    moved and records the change in the history. Behind the admin password like
    every ``/admin/`` route, and gated on processing being enabled.

    Args:
        employee_id: Staff Directory item id of the person being edited.
        body: The new values and the reason for the change.

    Returns:
        The service result: what changed and whether the history was recorded.

    Raises:
        HTTPException: 503 when processing is disabled, 400 on an invalid reason
            or value, 404 when the employee does not exist, 502 when the write to
            SharePoint failed.
    """
    if not settings.PROCESSING_ENABLED:               # same gate as set-balances
        raise HTTPException(status_code=503, detail="Processing is currently disabled")

    from app.services.staff_entitlements import (
        StaffWriteError,
        apply_entitlement_changes,
    )

    try:
        return await apply_entitlement_changes(
            employee_id, body.changes, body.reason, body.fields,
        )
    except LookupError as e:                           # employee not found
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:                            # bad reason or value
        raise HTTPException(status_code=400, detail=str(e))
    except StaffWriteError as e:                       # SharePoint write failed upstream
        raise HTTPException(status_code=502, detail=str(e))


class StaffExportRequest(BaseModel):
    """Body for the staff export: which people, columns and extra sheets.

    Attributes:
        employee_ids: Staff Directory item ids to list, in the order the table
            showed them (filters and sort already applied on the frontend).
            Omitted/null means everyone in name order; an empty list means no
            one (a filter matched nothing), so the sheet comes out empty.
        columns: The column keys HR chose (see ``staff_export.COLUMNS``); the
            name column is always included.
        include_summary: Add the per-department summary sheet.
        include_history: Add the change-history sheet.
    """
    employee_ids: list[str] | None = None
    columns: list[str] = []
    include_summary: bool = False
    include_history: bool = False


@router.post("/admin/staff/export")
async def admin_export_staff(body: StaffExportRequest):
    """Download the staff table as an Excel file (#277).

    Builds the workbook HR previews in the export dialog: a Staff sheet of the
    chosen people and columns in the table's order, with optional per-department
    summary and change-history sheets. Read only, so it is not gated on
    processing; behind the admin password like every ``/admin/`` route.

    Args:
        body: Which people, columns and extra sheets to include.

    Returns:
        The .xlsx file as an attachment.
    """
    from fastapi.responses import Response
    from app.services import staff_changes
    from app.services.app_settings import toronto_today
    from app.services.employee import _get_sp_user_name_map
    from app.services.manager_assignments import _extract_all_managers
    from app.services.staff_export import build_workbook, export_filename

    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)  # every employee
    by_id = {str(item.get("id")): item for item in items}

    # Keep the order the table sent. None means "everyone" (in name order); an
    # explicit list, even an empty one, means exactly those ids (a filter that
    # matched nobody exports an empty sheet rather than the whole directory).
    if body.employee_ids is None:
        ordered = sorted(items, key=lambda it: (it.get("fields", {}).get("Title") or "").lower())
    else:
        ordered = [by_id[i] for i in body.employee_ids if i in by_id]

    # Resolve each row's manager names once, so the Managers column reads as names.
    id_to_name = await _get_sp_user_name_map()
    rows = []
    for item in ordered:
        fields = item.get("fields", {})
        managers = []
        for mgr in _extract_all_managers(fields):          # [{sp_user_id, name}]
            name = mgr["name"] or id_to_name.get(mgr["sp_user_id"], "")
            if name:
                managers.append(name)
        rows.append({"id": item.get("id"), "fields": fields, "managers": managers})

    # The change-history sheet, only when asked for, for the listed people.
    history = []
    if body.include_history:
        history = await staff_changes.list_changes_for([str(r["id"]) for r in rows])

    today = toronto_today()
    data = build_workbook(rows, body.columns, body.include_summary, history)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{export_filename(today)}"'},
    )


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
