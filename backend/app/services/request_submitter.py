"""Who submitted a request: by email first, then by the person column.

Requests used to name their submitter only through a SharePoint person column
(SubmittedTest on leave, SubmittedBy on overtime and carryover). A person
column can only point at someone in the site's User Information List, which
fills when a person visits the site or is added to it. Staff who use the
public request page may never have visited, so their requests also carry the
submitter's email in a plain text column, SubmitterEmail, plus RequestSource
saying where the request came from.

Every place that needs "who is this request for" calls
``resolve_request_submitter`` here instead of reading the person column
directly, so a request with either kind of link resolves to the same Staff
Directory record. Older requests have no SubmitterEmail and keep resolving
through the person column exactly as before.

Writing the two columns is behind REQUEST_EMAIL_COLUMNS_ENABLED: SharePoint
refuses a whole item that names a column the list does not have, so the
setting stays off until ``ensure_request_columns`` (or an admin) has added
them to all three lists.
"""

import logging

from app.config import settings
from app.graph.client import graph_client
from app.graph.sharepoint import sp_client
from app.repositories.staff_store import staff_in_postgres
from app.services.employee import (
    get_employee_by_email,
    resolve_person_field,
    resolve_person_field_name,
)
from app.services.person_match import match_person

logger = logging.getLogger(__name__)

SUBMITTER_EMAIL_COLUMN = "SubmitterEmail"   # plain text: the submitter's address
REQUEST_SOURCE_COLUMN = "RequestSource"     # plain text: where the request came from


class UnknownSubmitter(ValueError):
    """A new request whose submitter is on no staff record, so it cannot be saved.

    Raised at intake once requests live in Postgres: the typed requests row needs
    the submitter's staff id (employee_id is not null), so a request that resolves
    to nobody is refused with a clear message instead of a half-written row. The
    message is safe to show the person.
    """

SOURCE_REQUEST_PAGE = "Request page"        # the public request page (#131)
SOURCE_API = "API"                          # the /api/forms endpoints
SOURCE_ADMIN = "Admin dashboard"            # an admin made it on the requests screen (#283)
SOURCE_MANAGER = "Manager dashboard"        # a manager logged a sick day for their team (#130)

# The person columns that name a request's submitter, per list.
SUBMITTER_PERSON_COLUMNS = ("SubmittedTest", "SubmittedBy")


def requests_linked_by_email() -> bool:
    """Whether new requests can carry SubmitterEmail, so no site membership is needed.

    True once requests live in Postgres (a row takes any field), or while they
    are still in SharePoint once REQUEST_EMAIL_COLUMNS_ENABLED says the lists
    have the two columns.

    This is a synchronous helper on the request-building path, so it reads the
    ``STORAGE_REQUESTS`` environment override rather than the live
    ``requests_storage`` database setting (which only the async
    ``requests_in_postgres`` reads). The override is what pins the backend in
    production, so this stays correct there; a runtime flip of the database
    setting alone would not reach here.

    Returns:
        True when a request can name its submitter by email.
    """
    override = (settings.STORAGE_REQUESTS or "").strip().lower()   # the environment pin, read synchronously
    return override == "postgres" or settings.REQUEST_EMAIL_COLUMNS_ENABLED


def submitter_columns(submitter_email: str | None, source: str | None) -> dict:
    """The extra fields to write on a new request item, when enabled.

    Args:
        submitter_email: The submitter's address, as verified or as given.
        source: One of the SOURCE_* values, or None to leave it blank.

    Returns:
        {"SubmitterEmail", "RequestSource"} with whatever is known, or an
        empty dict while requests_linked_by_email() is False.
    """
    if not requests_linked_by_email():
        return {}                                              # the SharePoint columns may not exist yet
    fields: dict = {}
    if submitter_email:
        fields[SUBMITTER_EMAIL_COLUMN] = submitter_email.strip().lower()
    if source:
        fields[REQUEST_SOURCE_COLUMN] = source
    return fields


def submitter_email_of(fields: dict) -> str:
    """The SubmitterEmail on a request item, normalised; "" when absent.

    Args:
        fields: The request item's SharePoint fields.

    Returns:
        The lowercase address, or "" for requests made before the column.
    """
    return (fields.get(SUBMITTER_EMAIL_COLUMN) or "").strip().lower()


async def resolve_request_submitter(fields: dict, person_column: str) -> dict | None:
    """Find the Staff Directory record a request belongs to.

    Args:
        fields: The request item's SharePoint fields.
        person_column: "SubmittedTest" for leave, "SubmittedBy" for overtime
            and carryover.

    Returns:
        The employee item ({"id", "fields"}), or None when neither the email
        nor the person column leads to one.
    """
    email = submitter_email_of(fields)
    # Once the Staff Directory is in Postgres, match by staff id then email only
    # (#265): no display-name match and no Microsoft 365 user-list lookup. The
    # request item carries the submitter's staff id in EmployeeID when known.
    if await staff_in_postgres():
        return await match_person(staff_id=fields.get("EmployeeID"), email=email)
    if email:
        employee = await get_employee_by_email(email)          # works without a site visit
        if employee:
            return employee
    # Fall back to the person column (every request made before the column).
    return await resolve_person_field(
        fields.get(person_column) or fields.get(f"{person_column}LookupId")
    )


async def resolve_request_submitter_name(fields: dict, person_column: str) -> str:
    """The submitter's display name, by email first, then the person column.

    Args:
        fields: The request item's SharePoint fields.
        person_column: As for resolve_request_submitter.

    Returns:
        The Staff Directory name, or "" when nothing resolves.
    """
    email = submitter_email_of(fields)
    # Postgres era (#265): resolve by staff id then email; take the name off the
    # matched record. No name or Microsoft 365 fallback.
    if await staff_in_postgres():
        matched = await match_person(staff_id=fields.get("EmployeeID"), email=email)
        return matched.get("fields", {}).get("Title", "") if matched else ""
    if email:
        employee = await get_employee_by_email(email)
        if employee:
            return employee.get("fields", {}).get("Title", "")
    return await resolve_person_field_name(
        fields.get(person_column) or fields.get(f"{person_column}LookupId")
    )


# ----- adding the columns -----

REQUEST_LISTS = (
    ("leave", "SP_LIST_LEAVE_REQUESTS"),
    ("overtime", "SP_LIST_OVERTIME_REQUESTS"),
    ("carryover-payout", "SP_LIST_CARRYOVER_PAYOUT"),
)


def _column_definition(name: str, description: str) -> dict:
    """A Graph columnDefinition for a single-line, optional text column.

    Args:
        name: The internal and display name.
        description: Shown in the list settings.

    Returns:
        The JSON body for POST /lists/{id}/columns.
    """
    return {
        "name": name,
        "displayName": name,
        "description": description,
        "enforceUniqueValues": False,
        "hidden": False,
        "indexed": False,
        "required": False,                                     # older items stay valid
        "text": {"allowMultipleLines": False, "maxLength": 255},
    }


_COLUMNS = (
    (SUBMITTER_EMAIL_COLUMN, "Email of the person the request is for (set by the request page)."),
    (REQUEST_SOURCE_COLUMN, "Where the request was made: Request page, API, or blank for the Microsoft Form."),
)


async def ensure_request_columns(create: bool) -> dict:
    """Check, and optionally add, SubmitterEmail and RequestSource on the lists.

    Reading needs the app's existing Sites permission; adding needs
    Sites.Manage.All. Each list is handled on its own, so one failure is
    reported without stopping the others.

    Args:
        create: False only reports; True also adds any missing column.

    Returns:
        {"lists": {request_type: {"present": [...], "added": [...],
        "missing": [...], "error": str | None}}, "ready": bool} where ready
        means every list now has both columns.
    """
    if not sp_client.site_id:
        await sp_client.resolve_site_id()                      # lazily, for a cold start
    report: dict = {}
    for request_type, setting_name in REQUEST_LISTS:
        list_id = getattr(settings, setting_name)
        entry = {"present": [], "added": [], "missing": [], "error": None}
        path = f"/sites/{sp_client.site_id}/lists/{list_id}/columns"
        try:
            data = await graph_client.get(path, params={"$select": "name"})
            existing = {c.get("name") for c in data.get("value", [])}
            for name, description in _COLUMNS:
                if name in existing:
                    entry["present"].append(name)
                elif create:
                    await graph_client.post(path, json=_column_definition(name, description))
                    entry["added"].append(name)
                    logger.info("Added column %s to the %s list", name, request_type)
                else:
                    entry["missing"].append(name)
        except Exception as e:  # noqa: BLE001 - reported per list, never raised
            logger.exception("Could not check request columns on the %s list", request_type)
            entry["error"] = str(e)
        report[request_type] = entry
    ready = all(
        not e["error"] and not e["missing"] and len(e["present"]) + len(e["added"]) == len(_COLUMNS)
        for e in report.values()
    )
    return {"lists": report, "ready": ready, "enabled": settings.REQUEST_EMAIL_COLUMNS_ENABLED}
