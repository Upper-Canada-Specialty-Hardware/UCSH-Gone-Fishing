"""Create a Staff Directory record from the dashboard, set up so requests work.

Adding an employee today means typing a new row straight into the Staff
Directory list. A row entered by hand can be missing anything, and a request
from a half-configured person then stalls with no obvious cause. This service is
the guided, validated version: it writes exactly the columns the code reads,
refuses input that would not work, and leaves the record in a state the admin
setup check passes by construction.

It follows the shape the request forms already use — a pure function that
validates and assembles the field payload (no I/O, trivially testable), and a
thin async wrapper that resolves identities and writes to SharePoint. The set of
columns written was derived by tracing what the code actually reads; the list's
deprecated columns (Supervisor, SupervisorLink, TitleLink, System Check,
Comments, Extension, Birthday) are deliberately left unset.
"""

import logging

from app.repositories import get_employee_repository  # data-access seam: SharePoint today, Postgres after cutover
from app.services.employee import (
    LOCATION_PROVINCE_MAP,
    get_employee_by_name,
    map_location_to_province,
)
from app.services.balance import recalculate_request_allow_date
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.manager_assignments import update_employee_managers

logger = logging.getLogger(__name__)

# SalaryHourly is not free text — the balance engine branches on the exact
# string "Hourly", so only these two values behave predictably.
SALARY_HOURLY_CHOICES = ("Salary", "Hourly")

class EmployeeValidationError(ValueError):
    """Raised when the submitted employee cannot become a working record.

    Carries a message written for the person filling in the form, so the route
    can surface it directly rather than turning it into a generic 500.
    """


def _num(value, field: str) -> float:
    """Read a numeric input, rejecting anything that is not a number.

    Args:
        value: The submitted value.
        field: Human name of the field, for the error message.

    Returns:
        The value as a float.

    Raises:
        EmployeeValidationError: When it will not parse.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        raise EmployeeValidationError(f"{field} must be a number.")


def build_employee_fields(form_data: dict) -> dict:
    """Validate the form and assemble the Staff Directory field payload. No I/O.

    Everything here is checkable without touching SharePoint: the required
    fields are present, the location maps to a province, the employment type is
    one the balance engine understands, and the entitlements are above zero.
    Identity resolution (email and managers) happens in the async wrapper, since
    it needs list reads.

    Args:
        form_data: The submitted form, snake_case keys.

    Returns:
        The field dict to hand to create_list_item, minus the person and
        computed fields the wrapper adds.

    Raises:
        EmployeeValidationError: On any invalid or missing input.
    """
    name = (form_data.get("title") or "").strip()
    if not name:
        raise EmployeeValidationError("A name is required.")

    email = (form_data.get("email_address") or "").strip()
    if not email:
        raise EmployeeValidationError("An email address is required.")

    location = (form_data.get("location") or "").strip()
    if location not in LOCATION_PROVINCE_MAP:
        raise EmployeeValidationError(
            f"Location must be one of: {', '.join(LOCATION_PROVINCE_MAP)}."
        )
    map_location_to_province(location)  # cannot raise given the check above; kept as the single source of truth

    department = (form_data.get("department") or "").strip()
    if not department:
        raise EmployeeValidationError("A department is required.")

    salary_hourly = (form_data.get("salary_hourly") or "").strip()
    if salary_hourly not in SALARY_HOURLY_CHOICES:
        raise EmployeeValidationError(
            f"Employment type must be one of: {', '.join(SALARY_HOURLY_CHOICES)}."
        )

    vacation_entitlement = _num(form_data.get("vacation_entitlement"), "Yearly vacation entitlement")
    sick_entitlement = _num(form_data.get("sick_entitlement"), "Sick day entitlement")
    if vacation_entitlement <= 0:
        raise EmployeeValidationError("Yearly vacation entitlement must be above zero.")
    if sick_entitlement <= 0:
        raise EmployeeValidationError("Sick day entitlement must be above zero.")

    fields = {
        "Title": name,
        "EmailAddress": email,
        "Location": location,
        "Department": department,
        "SalaryHourly": salary_hourly,
        "DefaultYearlyVacationDays": vacation_entitlement,
        "SickDayEntitlement": sick_entitlement,
    }

    # Opening balances, each defaulting to zero. A new hire normally starts at
    # zero everywhere; the form can seed a mid-year hire's carried figures.
    balance_inputs = {
        "CurrentVacationBalance": "vacation_balance",
        "CurrentSickDayBalance": "sick_balance",
        "CurrentOvertimeBalance": "overtime_balance",
        "CarryOver": "carry_over",
        "Payout": "payout",
    }
    for column, form_key in balance_inputs.items():
        fields[column] = _num(form_data.get(form_key), column)

    # An optional contact number, kept only because it is what lets a manager
    # approve by text later. Not required for a plain employee.
    cell = (form_data.get("cell_number") or "").strip()
    if cell:
        fields["CellNumber"] = cell

    return fields


async def create_employee(form_data: dict, manager_sp_user_ids: list[int]) -> dict:
    """Create a fully set-up Staff Directory record and return it.

    Args:
        form_data: The submitted form.
        manager_sp_user_ids: SharePoint user ids of the supervisor(s) to record
            in AllManagers. The manager dashboard passes the creating manager;
            the admin dashboard passes whoever the admin picked.

    Returns:
        The created record in the {"id", "fields"} shape.

    Raises:
        EmployeeValidationError: On invalid input, an email that does not resolve
            to a Microsoft 365 account, or a name already in the directory.
    """
    fields = build_employee_fields(form_data)

    # The name is how a request is routed back to a person, so a second record
    # with the same name would make routing ambiguous for both.
    if await get_employee_by_name(fields["Title"]):
        raise EmployeeValidationError(
            f"An employee named '{fields['Title']}' already exists in the directory."
        )

    # Identity check: the email must resolve to a Microsoft 365 account, or
    # nothing links the record to the person and their requests never match.
    # IT provisions the account before onboarding, so a miss is a real error.
    if not await _resolve_user_lookup_id(fields["EmailAddress"]):
        raise EmployeeValidationError(
            f"No Microsoft 365 account was found for {fields['EmailAddress']}. "
            "Check the address, or that IT has set up their account."
        )

    if not manager_sp_user_ids:
        raise EmployeeValidationError("At least one supervisor must be assigned.")

    item = await get_employee_repository().create(fields)  # write through the seam, not sp_client
    employee_id = item["id"]  # SP item id (str), used below for managers + re-read
    logger.info("Created Staff Directory record #%s (%s)", employee_id, fields["Title"])

    # Supervisors are a Person/Group field, written separately in the lookup-id
    # shape Graph requires — the same path update_employee_managers uses.
    await update_employee_managers(int(employee_id), manager_sp_user_ids)

    # Request Allow Date is derived from the opening vacation and carry-over,
    # by the one function that already owns that rule.
    await recalculate_request_allow_date(
        employee_id, fields["CurrentVacationBalance"], fields["CarryOver"]
    )

    # Re-read through the seam so the caller sees the person + computed fields
    # the two calls above added. get_by_id returns None on a read miss (the old
    # get_list_item raised); the record was just created, so a miss here is a
    # real failure, not an empty result to hand back as a "created" employee.
    created = await get_employee_repository().get_by_id(employee_id)  # {"id","fields"} | None
    if created is None:  # created but unreadable -> internal error, not user input
        raise RuntimeError(f"Employee #{employee_id} was created but could not be read back.")
    return created
