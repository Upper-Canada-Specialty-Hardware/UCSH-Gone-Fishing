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

from app.config import settings
from app.repositories.staff_store import staff_store
from app.services.employee import (
    LOCATION_PROVINCE_MAP,
    map_location_to_province,
)
from app.services.balance import recalculate_request_allow_date
from app.services.employment import HOURLY, HOURLY_BC, HOURLY_TYPES, SALARY
from app.services.manager_assignments import update_employee_managers
from app.services.staff_cleanup import clean_email, clean_employee_form, collapse_whitespace

logger = logging.getLogger(__name__)

# SalaryHourly is not free text: the balance engine branches on these exact
# strings (services/employment.py), so only these values behave predictably.
SALARY_HOURLY_CHOICES = (SALARY, HOURLY, HOURLY_BC)

class EmployeeValidationError(ValueError):
    """Raised when the submitted employee cannot become a working record.

    Carries a message written for the person filling in the form, so the route
    can surface it directly rather than turning it into a generic 500.
    """


class EmployeeNameClash(EmployeeValidationError):
    """The name is already on staff: the person already has a record (#444).

    Carries the existing record's id so the route can answer 409 and the frontend
    can open that person's profile (if the viewer may see it) instead of just
    showing the old "already exists" error.

    Attributes:
        existing_id: The Staff Directory id of the record that already has the name.
    """

    def __init__(self, message: str, existing_id: str):
        super().__init__(message)
        self.existing_id = existing_id


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
    name = collapse_whitespace(form_data.get("title"))  # trims ends + squeezes doubled/NBSP spaces
    if not name:
        raise EmployeeValidationError("A name is required.")

    email = clean_email(form_data.get("email_address"))  # no spaces anywhere, lower-cased
    if not email:
        raise EmployeeValidationError("An email address is required.")

    location = (form_data.get("location") or "").strip()
    if location not in LOCATION_PROVINCE_MAP:
        raise EmployeeValidationError(
            f"Location must be one of: {', '.join(LOCATION_PROVINCE_MAP)}."
        )
    map_location_to_province(location)  # cannot raise given the check above; kept as the single source of truth

    department = collapse_whitespace(form_data.get("department"))  # same tidy as the name
    if not department:
        raise EmployeeValidationError("A department is required.")

    salary_hourly = (form_data.get("salary_hourly") or "").strip()
    if salary_hourly not in SALARY_HOURLY_CHOICES:
        raise EmployeeValidationError(
            f"Employment type must be one of: {', '.join(SALARY_HOURLY_CHOICES)}."
        )

    vacation_entitlement = _num(form_data.get("vacation_entitlement"), "Yearly vacation entitlement")
    sick_entitlement = _num(form_data.get("sick_entitlement"), "Sick day entitlement")
    if salary_hourly in HOURLY_TYPES:                  # hourly staff have no vacation (#300)
        if vacation_entitlement < 0:
            raise EmployeeValidationError("Yearly vacation entitlement cannot be below zero.")
    elif vacation_entitlement <= 0:
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


async def find_directory_clashes(name: str, email: str) -> dict:
    """Existing staff records that clash with this name or email, loosely matched.

    One read of the Staff Directory (through the store, so it follows
    ``staff_storage``). The comparison runs both sides through the same cleaner,
    so a stored name with a trailing or doubled space (the Glenn Clarke case)
    still counts as the same person, and an email clashes regardless of case or
    stray spaces. The first match for each is returned, which is the record the
    request path would also resolve to.

    Args:
        name: The new hire's name, already cleaned.
        email: The new hire's email, already cleaned.

    Returns:
        ``{"name": <record or None>, "email": <record or None>}`` - the first
        existing record whose cleaned name (resp. email) equals the input.
    """
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)  # one directory read
    name_key = collapse_whitespace(name).lower()     # case-insensitive, space-tidied name key
    email_key = clean_email(email)                   # already lower + space-free
    found = {"name": None, "email": None}            # first hit for each, or None
    for item in items:                               # scan once, filling both in a single pass
        fields = item.get("fields", {})
        if found["name"] is None and name_key and collapse_whitespace(fields.get("Title", "")).lower() == name_key:
            found["name"] = item                     # same person by name
        if found["email"] is None and email_key and clean_email(fields.get("EmailAddress", "")) == email_key:
            found["email"] = item                    # same person by email
    return found


async def create_employee(
    form_data: dict, manager_sp_user_ids: list[int],
    *, history_source: str = "admin_dashboard", history_actor: str | None = "HR",
) -> dict:
    """Create a fully set-up Staff Directory record and return it.

    Args:
        form_data: The submitted form.
        manager_sp_user_ids: SharePoint user ids of the supervisor(s) to record
            in AllManagers. The manager dashboard passes the creating manager;
            the admin dashboard passes whoever the admin picked.
        history_source: The channel to record the supervisor assignment under in
            the new person's history; the admin dashboard by default, or
            "manager_dashboard" when a manager adds the hire (#447). Both are human
            actions, so neither is the automatic "system" default.
        history_actor: Who created the record: "HR" for the shared admin dashboard,
            the manager's own name when a manager adds the hire.

    Returns:
        The created record in the {"id", "fields"} shape, plus "notices": a
        list of readable notes for the person who added them (empty when
        everything is linked).

    Raises:
        EmployeeValidationError: On invalid input, or a name or email already
            in the directory.
    """
    fields = build_employee_fields(form_data)

    # One directory read answers both duplicate questions, loosely matched so a
    # stored name with a stray space does not sneak a second record past us.
    clashes = await find_directory_clashes(fields["Title"], fields["EmailAddress"])

    # The name is how a request is routed back to a person, so a second record
    # with the same name would make routing ambiguous for both. The existing id
    # is carried so the dashboard can open that person instead (#444).
    if clashes["name"]:
        raise EmployeeNameClash(
            f"An employee named '{fields['Title']}' already exists in the directory.",
            existing_id=str(clashes["name"]["id"]),
        )

    # The email is now how requests from the request page find this person, so
    # a second record with the same email would make that ambiguous too.
    if clashes["email"]:
        raise EmployeeValidationError(
            f"An employee with the email {fields['EmailAddress']} already exists in the directory."
        )

    # No notice is raised today; the key stays so the response keeps its shape.
    notices: list[str] = []

    if not manager_sp_user_ids:
        raise EmployeeValidationError("At least one supervisor must be assigned.")

    # Create through the store: on Postgres this mints the new staff id there
    # (nothing goes to SharePoint); on SharePoint it creates the item directly.
    item = await staff_store.create_list_item(settings.SP_LIST_STAFF_DIRECTORY, fields)
    employee_id = item["id"]
    logger.info("Created Staff Directory record #%s (%s)", employee_id, fields["Title"])

    # Supervisors are a Person/Group field, written separately in the lookup-id
    # shape Graph requires, the same path update_employee_managers uses. Record
    # who added the hire (HR, or the manager) rather than the "system" default,
    # so the person's history names a human for their first supervisor (#447).
    await update_employee_managers(
        int(employee_id), manager_sp_user_ids,
        history_source=history_source,
        history_reason="Supervisor set when the record was created.",
        history_actor=history_actor,
    )

    # Request Allow Date is derived from the opening vacation and carry-over,
    # by the one function that already owns that rule.
    await recalculate_request_allow_date(
        employee_id, fields["CurrentVacationBalance"], fields["CarryOver"]
    )

    # Return the record as written, re-read so the caller sees the person and
    # computed fields the two calls above added (through the store, so it reads the
    # backend the create landed in).
    record = await staff_store.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, employee_id)
    return {**record, "notices": notices}


def _problem(code: str, level: str, title: str, detail: str) -> dict:
    """One entry for the check endpoint's problems list.

    Args:
        code: Stable identifier the frontend can key wording off.
        level: ``"block"`` (the create route will refuse it) or ``"warn"``
            (worth telling the person, but the create still goes through).
        title: Short plain-language summary.
        detail: The specifics, written for whoever is adding the person.

    Returns:
        A ``{"code", "level", "title", "detail"}`` dict.
    """
    return {"code": code, "level": level, "title": title, "detail": detail}


async def check_employee(
    form_data: dict, *, has_supervisor: bool
) -> dict:
    """Dry-run the add-employee checks without writing anything.

    Produces the cleaned form, the list of fields cleaning changed, and every
    problem the create would hit. Blocks are the four things the create route
    refuses outright (an invalid form, a duplicate name, a duplicate email, no
    supervisor); the one warn is a notice the create only reports (no holidays
    for the person's province). Nothing here writes: it reads the directory
    and the holiday list only.

    Args:
        form_data: The submitted form, snake_case keys (the same body the create
            route takes).
        has_supervisor: Whether the caller resolved at least one supervisor for
            this new hire (the admin picks them; the manager route uses the
            signed-in manager).

    Returns:
        ``{"cleaned": {...}, "changes": [{"field","before","after"}, ...],
        "problems": [{"code","level","title","detail"}, ...]}``.
    """
    cleaned, changes = clean_employee_form(form_data)   # tidy the identity fields, note what moved
    problems: list[dict] = []                           # blocks and warns, in the order found

    # The form must assemble into a valid record first; without that every later
    # check would be guessing. A failure here is the single blocking problem.
    try:
        fields = build_employee_fields(form_data)       # cleans + validates, no I/O
    except EmployeeValidationError as e:
        problems.append(_problem(
            "invalid_form", "block", "Some details still need fixing", str(e),
        ))
        return {"cleaned": cleaned, "changes": changes, "problems": problems}

    # Loosely-matched duplicates: the same two refusals the create makes.
    clashes = await find_directory_clashes(fields["Title"], fields["EmailAddress"])
    if clashes["name"]:
        problems.append(_problem(
            "same_name", "block", "That name is already in the directory",
            f"An employee named '{fields['Title']}' already exists, so requests "
            "could route to the wrong person. Use a distinguishing name.",
        ))
    if clashes["email"]:
        problems.append(_problem(
            "same_email", "block", "That email is already in the directory",
            f"An employee with the email {fields['EmailAddress']} already exists, "
            "so request-page submissions could not tell them apart.",
        ))

    # A record with no supervisor has nobody to approve its requests.
    if not has_supervisor:
        problems.append(_problem(
            "no_supervisor", "block", "No supervisor chosen",
            "Choose at least one supervisor so this person's requests have "
            "someone to approve them.",
        ))

    # --- warnings: the create still goes through, but these are worth knowing ---

    # No holidays for the province means every weekday is counted as a workday,
    # which over-deducts leave. Location already validated above, so this maps.
    from app.services.holidays import get_holidays_for_province   # lazy: avoids an import cycle
    province = map_location_to_province(fields["Location"])        # safe: build_employee_fields passed
    if not await get_holidays_for_province(province):
        problems.append(_problem(
            "no_holidays", "warn", f"No holidays set for {province}",
            f"The {province} calendar has no holidays, so every weekday counts as "
            "a workday when their leave is calculated.",
        ))

    return {"cleaned": cleaned, "changes": changes, "problems": problems}
