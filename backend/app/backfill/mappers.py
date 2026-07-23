"""SharePoint item -> Postgres column-values mappers for the backfill.

Each mapper turns one SharePoint list item (``{"id": ..., "fields": {...}}``)
into a dict of column values for the matching business model, keyed on
``sp_item_id``. The SP field name behind each column is documented in the
``# SP column`` comments on the models (app/models/*.py); these mappers apply
that mapping plus light type coercion.

Field-name confidence:
  * CONFIRMED (used by the live services today, so known-correct): the identity
    fields, the five balance pots + entitlements, dates, statuses, Days/Hours,
    TypeofRequest/SystemState/EmployeeID, and the Person/Group prefixes
    (SubmittedTest / SubmittedBy / Manager).
  * BEST-EFFORT (internal name inferred from the model comment / list schema,
    not yet confirmed against a live item): employee_type, staff_location,
    staff_department, partial_hours, notes. These are all nullable, non-critical
    annotation fields. A wrong internal name resolves to None on BOTH the SP and
    PG side, so ``verify`` cannot flag it — confirm these against one live item's
    raw fields before relying on them (see the PR notes).

Pure functions — no I/O — so they unit-test directly against fixtures.
"""
from datetime import date, datetime


def _to_float(value):
    """Coerce an SP numeric-ish value to float, or None for blank/unparseable."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_float0(value):
    """Like ``_to_float`` but blank/None -> 0.0. Used for the non-null balance
    pots and entitlements (the columns are NOT NULL, default 0.0)."""
    f = _to_float(value)
    return 0.0 if f is None else f


def _to_date(value):
    """Parse an SP date value (ISO string, possibly with a time part / trailing
    'Z') to a ``date``. None/unparseable -> None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _to_str(value):
    return None if value is None else str(value)


def _extract_lookup_id(fields: dict, prefix: str) -> int | None:
    """SP Person/Group lookup id from either shape a Graph list item can carry:
    an explicit ``{prefix}LookupId`` scalar (form-created items) or a nested
    ``{prefix: {"LookupId": ...}}`` object (SP-created items).

    Mirrors ``services.overlap_detection._extract_lookup_id`` deliberately — kept
    local so the backfill tool has no dependency on a service's private helper.
    """
    lid = fields.get(f"{prefix}LookupId")
    if lid is not None:
        try:
            return int(lid)
        except (ValueError, TypeError):
            pass
    nested = fields.get(prefix)
    if isinstance(nested, dict):
        try:
            return int(nested["LookupId"])
        except (KeyError, ValueError, TypeError):
            pass
    return None


def map_employee(item: dict) -> dict:
    f = item.get("fields", {})
    return {
        "sp_item_id": str(item["id"]),
        "email": f.get("EmailAddress"),
        # sp_user_lookup_id needs an email -> User Information List resolution (a
        # live Graph call). That identity linkage is populated in the employee
        # cutover (PR F); backfill leaves it null.
        "sp_user_lookup_id": None,
        "name": f.get("Title") or "",
        "department": f.get("Department"),
        "location": f.get("Location"),
        "employee_type": f.get("EmployeeType"),
        "vacation_balance": _to_float0(f.get("CurrentVacationBalance")),
        "sick_balance": _to_float0(f.get("CurrentSickDayBalance")),
        "overtime_balance": _to_float0(f.get("CurrentOvertimeBalance")),
        "carryover_balance": _to_float0(f.get("CarryOver")),
        "payout_balance": _to_float0(f.get("Payout")),
        "vacation_entitlement": _to_float0(f.get("DefaultYearlyVacationDays")),
        "sick_entitlement": _to_float0(f.get("SickDayEntitlement")),
        "request_allow_date": _to_date(f.get("RequestAllowDate")),
    }


def map_holiday(item: dict) -> dict:
    f = item.get("fields", {})
    return {
        "sp_item_id": str(item["id"]),
        "title": f.get("Title"),
        "date": _to_date(f.get("Date")),
        "province": f.get("Province"),
    }


def map_leave_request(item: dict) -> dict:
    f = item.get("fields", {})
    return {
        "sp_item_id": str(item["id"]),
        "leave_type": f.get("LeaveType"),
        "status": f.get("Status"),
        "approve_processed_flag": f.get("ApproveProcessedFlag"),
        "start_date": _to_date(f.get("StartDate")),
        "end_date": _to_date(f.get("EndDate")),
        "days": _to_float(f.get("Days")),
        "partial_hours": _to_float(f.get("PartialHours")),
        "title": f.get("Title"),
        "notes": f.get("Notes"),
        # Requester person field is "SubmittedTest"; assigned manager is "Manager".
        "submitter_sp_user_lookup_id": _extract_lookup_id(f, "SubmittedTest"),
        # Person/Group fields carry no display name (Graph returns LookupId only),
        # so the name is resolved later, not at backfill.
        "submitter_name": None,
        "manager_sp_user_lookup_id": _extract_lookup_id(f, "Manager"),
        "staff_location": f.get("StaffLocation"),
        "staff_department": f.get("StaffDepartment"),
    }


def map_overtime_request(item: dict) -> dict:
    f = item.get("fields", {})
    return {
        "sp_item_id": str(item["id"]),
        "title": f.get("Title"),
        "date": _to_date(f.get("StartDate")),
        "hours": _to_float(f.get("Hours")),
        "status": f.get("Status"),
        "submitter_sp_user_lookup_id": _extract_lookup_id(f, "SubmittedBy"),
        "submitter_name": None,
        "manager_sp_user_lookup_id": _extract_lookup_id(f, "Manager"),
    }


def map_carryover_payout_request(item: dict) -> dict:
    f = item.get("fields", {})
    return {
        "sp_item_id": str(item["id"]),
        "type_of_request": f.get("TypeofRequest"),
        "days": _to_float(f.get("Days")),
        "system_state": f.get("SystemState"),
        "submitter_sp_user_lookup_id": _extract_lookup_id(f, "SubmittedBy"),
        "employee_sp_item_id": _to_str(f.get("EmployeeID")),
        "manager_sp_user_lookup_id": _extract_lookup_id(f, "Manager"),
    }
