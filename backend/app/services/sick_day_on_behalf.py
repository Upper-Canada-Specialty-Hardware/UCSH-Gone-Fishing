"""Manager-logged sick days: a manager records a sick day for a team member.

When staff call in sick they are away from a computer, and the manager ends
up reminding them to log it once they are back. This lets the manager log it
from the Team dashboard, and the system approves it on the spot.

What gets written is an ordinary leave request, LeaveType "Sick or Personal
Day", created in the Leave Requests list like every other request, which is
then approved through ``approve_leave_request``, the same handler behind the
email link, the SMS reply and the dashboard Approve button. The balance
deduction (for BC hourly, whole sick days from sick only, which may go below
zero; for salaried staff, the usual cascade), the exactly-once claim, the
per-employee lock, the audit log and the employee's confirmation emails are
therefore all the existing ones; nothing here touches a balance directly.

Two gates decide whether a manager may do this:

1. **Province gate.** The manager's own ``Location`` on the Staff Directory
   must map to a province listed in ``settings.SICK_DAY_ON_BEHALF_PROVINCES``.
   British Columbia only to start; Ontario is added there once HR agrees.
2. **Assignment gate.** The employee must list the manager in ``AllManagers``.

Every check that can refuse the submission runs before anything is written, so
a refused submission leaves no trace. The created request is stamped with the
acting manager (``ManagerLookupId``) and its description reads
"Logged by <manager>", so the provenance shows in the emails, the history
grids and the audit dialog.
"""

import logging

from app.config import settings
from app.repositories.request_store import request_store
from app.services.business_days import calculate_business_days
from app.services.employee import (
    LOCATION_PROVINCE_MAP,
    get_employee_by_id,
    map_location_to_province,
)
from app.services.holidays import (
    _parse_date,
    get_half_friday_season,
    get_holidays_for_province,
)
from app.services.leave_requests import _resolve_user_lookup_id, approve_leave_request
from app.services.overlap_detection import check_leave_overlap, describe_leave_conflict
from app.services.request_descriptions import compose_leave_title
from app.services.request_submitter import SOURCE_MANAGER, submitter_columns

logger = logging.getLogger(__name__)

# The LeaveType approve_leave_request deducts from the Sick pot.
SICK_LEAVE_TYPE = "Sick or Personal Day"


class SickDayOnBehalfError(ValueError):
    """The submission cannot become an approved sick day; the message says why."""


class SickDayNotPermitted(PermissionError):
    """The manager's location is outside the provinces this is enabled for."""


def allowed_provinces() -> set[str]:
    """Province codes whose managers may log sick days, read from settings.

    Returns:
        Upper-cased, whitespace-trimmed codes; empty when the setting is blank,
        which disables the feature everywhere.
    """
    raw = settings.SICK_DAY_ON_BEHALF_PROVINCES or ""  # blank setting -> nothing enabled
    return {code.strip().upper() for code in raw.split(",") if code.strip()}  # tolerate "bc, on"


def manager_may_log_sick_days(manager_fields: dict) -> bool:
    """Whether a manager's Staff Directory Location passes the province gate.

    Args:
        manager_fields: The manager's Staff Directory fields (reads Location).

    Returns:
        True when the location maps to an enabled province. A blank or
        unmapped location is False rather than an error: this gate only ever
        decides whether the button is shown.
    """
    location = (manager_fields.get("Location") or "").strip()
    province = LOCATION_PROVINCE_MAP.get(location)  # None when unmapped; no raise here
    return bool(province) and province.upper() in allowed_provinces()


def is_assigned_to(employee_fields: dict, manager_name: str) -> bool:
    """Whether the employee lists this manager in their AllManagers field.

    Args:
        employee_fields: The employee's Staff Directory fields.
        manager_name: The manager's Title, as AllManagers stores LookupValue.

    Returns:
        True when the manager is one of the employee's supervisors.
    """
    all_managers = employee_fields.get("AllManagers")
    if not all_managers or not isinstance(all_managers, list):  # no supervisors at all
        return False
    for entry in all_managers:
        name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""  # Graph's person shape
        if name and name == manager_name:
            return True
    return False


def build_sick_day_fields(form_data: dict, employee_name: str, manager_name: str) -> dict:
    """Validate the form and assemble the leave-request fields. No I/O.

    Args:
        form_data: ``{"start_date": "YYYY-MM-DD", "end_date": optional,
            "notes": optional}``. A missing end date means a single day.
        employee_name: Staff Directory Title of the employee taking the day.
        manager_name: Title of the manager logging it; recorded in the
            description so the provenance is visible everywhere.

    Returns:
        The core fields of the new Leave Requests item. Days, the person
        fields and the manager stamp are added by ``log_sick_day_on_behalf``.

    Raises:
        SickDayOnBehalfError: A missing or unreadable date, or an end date
            before the start date.
    """
    start_raw = str(form_data.get("start_date") or "").strip()
    end_raw = str(form_data.get("end_date") or "").strip() or start_raw  # no end -> single day
    start = _parse_date(start_raw)  # None on anything that is not a date
    end = _parse_date(end_raw)
    if not start:
        raise SickDayOnBehalfError("A start date is required (YYYY-MM-DD).")
    if not end:
        raise SickDayOnBehalfError("The end date must be a valid date (YYYY-MM-DD).")
    if end < start:
        raise SickDayOnBehalfError("The end date must be on or after the start date.")

    notes = str(form_data.get("notes") or "").strip()
    provenance = f"Logged by {manager_name}"  # becomes the request description
    description = f"{provenance}: {notes}" if notes else provenance

    return {
        "Title": compose_leave_title(employee_name, description),  # "<name> /// Logged by ..."
        "LeaveType": SICK_LEAVE_TYPE,
        "Status": "Pending",  # approve_leave_request insists on Pending + Not Processed
        "ApproveProcessedFlag": "Not Processed",
        "StartDate": start.isoformat(),  # normalised to YYYY-MM-DD
        "EndDate": end.isoformat(),
    }


def _manager_stamp(emp_fields: dict, manager_lookup_id: int | None) -> dict:
    """The org-placement and manager fields stamped onto the new request.

    The same columns ``auto_assign_manager`` writes for a form-submitted
    request, except that the Manager is the acting manager rather than the
    first supervisor listed, so the request records who actually logged it.

    Args:
        emp_fields: The employee's Staff Directory fields.
        manager_lookup_id: The acting manager's Microsoft 365 lookup id, or
            None when their email did not resolve (the stamp then omits it).

    Returns:
        The field dict for ``update_list_item_fields``.
    """
    stamp = {
        "StaffLocation": emp_fields.get("Location", ""),
        "StaffDepartment": emp_fields.get("Department", ""),
    }
    if manager_lookup_id:
        stamp["ManagerLookupId"] = manager_lookup_id  # Person field, written by lookup id
    all_managers = emp_fields.get("AllManagers")
    if all_managers and isinstance(all_managers, list):
        lookup_ids = [
            int(entry["LookupId"]) for entry in all_managers
            if isinstance(entry, dict) and entry.get("LookupId")
        ]
        if lookup_ids:  # multi-value Person field needs the collection type hint
            stamp["AllManagersLookupId@odata.type"] = "Collection(Edm.Int32)"
            stamp["AllManagersLookupId"] = lookup_ids
    return stamp


async def log_sick_day_on_behalf(manager_id: str | int, form_data: dict) -> dict:
    """Record and approve a sick day for one of the manager's employees.

    Order of operations: both gates, the dates, the working-day count, the
    employee's identity and the clash check all run first, so a refused
    submission writes nothing. Only then is the request created, stamped with
    the manager, and approved through the shared approval handler.

    Args:
        manager_id: Staff Directory id of the signed-in manager (from the
            dashboard token).
        form_data: ``{"employee_id", "start_date", "end_date"?, "notes"?}``.

    Returns:
        ``{"status": "approved", "request_id", "employee_name", "days",
        "balances"}``, ``balances`` is what the approval handler returned
        after the deduction and cascade.

    Raises:
        SickDayNotPermitted: The manager's location is not enabled.
        SickDayOnBehalfError: Anything else that stops it, unknown employee,
            not on the manager's team, bad dates, no working days, a clash
            with approved leave, or an approval the shared handler refused
            (the request then exists and waits in the manager's Pending list).
    """
    manager = await get_employee_by_id(manager_id)
    if not manager:
        raise SickDayOnBehalfError("Your own staff record could not be found.")
    mgr_fields = manager["fields"]
    manager_name = (mgr_fields.get("Title") or "").strip()
    if not manager_may_log_sick_days(mgr_fields):  # province gate, on the manager's own location
        raise SickDayNotPermitted(
            "Logging sick days for your team is not enabled for your location."
        )

    employee_id = str(form_data.get("employee_id") or "").strip()
    if not employee_id:
        raise SickDayOnBehalfError("Choose the employee the sick day is for.")
    employee = await get_employee_by_id(employee_id)
    if not employee:
        raise SickDayOnBehalfError("That employee could not be found in the Staff Directory.")
    emp_fields = employee["fields"]
    employee_name = (emp_fields.get("Title") or "").strip()
    if not is_assigned_to(emp_fields, manager_name):  # assignment gate
        raise SickDayOnBehalfError(
            f"{employee_name} is not assigned to you, so you cannot log a sick day for them."
        )

    fields = build_sick_day_fields(form_data, employee_name, manager_name)
    start = _parse_date(fields["StartDate"])
    end = _parse_date(fields["EndDate"])

    # Working days are counted here rather than left to auto_calculate_days so
    # a range with no working days can be refused before anything is written.
    try:
        province = map_location_to_province(emp_fields.get("Location", ""))  # raises when unmapped
    except ValueError:
        raise SickDayOnBehalfError(
            f"{employee_name}'s location has no province on file, so working days cannot be counted."
        )
    holidays = await get_holidays_for_province(province)
    days = calculate_business_days(start, end, holidays, get_half_friday_season(holidays))
    if days <= 0:  # weekend-only or holiday-only range would deduct nothing
        raise SickDayOnBehalfError(
            "That date range has no working days (weekends and company holidays are not counted)."
        )
    fields["Days"] = days

    # The request is recorded in the employee's name: approval resolves the
    # employee from this person field, so it must resolve or nothing works.
    submitter_lookup_id = await _resolve_user_lookup_id(emp_fields.get("EmailAddress", ""))
    if not submitter_lookup_id:
        raise SickDayOnBehalfError(
            f"{employee_name}'s email does not resolve to a Microsoft 365 account, "
            "so the request cannot be recorded in their name."
        )
    fields["SubmittedTestLookupId"] = submitter_lookup_id
    # SubmitterEmail and RequestSource, like every other intake path, so the
    # request resolves by email too and reads as logged from the manager screen.
    fields.update(submitter_columns(emp_fields.get("EmailAddress", ""), SOURCE_MANAGER))

    # Same rule as a manager approving by hand: approved leave on any of these
    # dates blocks it. Checked before creation so a clash leaves nothing behind.
    conflict = await check_leave_overlap(
        submitter_lookup_id=submitter_lookup_id,
        start_date=fields["StartDate"],
        end_date=fields["EndDate"],
        days=days,
        submitter_email=emp_fields.get("EmailAddress", ""),
    )
    if conflict:
        raise SickDayOnBehalfError(
            f"{employee_name} already has approved leave on those dates. "
            f"{describe_leave_conflict(conflict)}"
        )

    manager_lookup_id = await _resolve_user_lookup_id(mgr_fields.get("EmailAddress", ""))
    if not manager_lookup_id:  # approval does not need it; the stamp just omits the Manager
        logger.warning(
            "Manager %s has no Microsoft 365 lookup id; sick day for %s will carry no Manager field",
            manager_name, employee_name,
        )

    # request_store, not SharePoint directly: request lists may live in Postgres.
    item = await request_store.create_list_item(settings.SP_LIST_LEAVE_REQUESTS, fields)
    request_id = item["id"]
    logger.info(
        "Manager %s logged a %s-day sick day (#%s) for %s", manager_name, days, request_id, employee_name,
    )

    # Two writes (create, then stamp) rather than one, mirroring the proven
    # form-intake sequence for the Person/Group columns.
    await request_store.update_list_item_fields(
        settings.SP_LIST_LEAVE_REQUESTS, request_id, _manager_stamp(emp_fields, manager_lookup_id),
    )

    result = await approve_leave_request(request_id, manager_id)  # the shared, exactly-once path
    if "error" in result:
        # The request exists, Pending, with days and a manager, so it shows in
        # the manager's Pending list and can be approved from there.
        raise SickDayOnBehalfError(
            f"The sick day was recorded as request #{request_id} but could not be "
            f"approved automatically: {result['error']} It is waiting in your Pending list."
        )

    return {
        "status": "approved",
        "request_id": str(request_id),
        "employee_name": employee_name,
        "days": days,
        "balances": result.get("balances"),
    }
