import logging

from app.config import settings
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)

LOCATION_PROVINCE_MAP = {
    "Toronto Victoria Park": "ON",
    "Toronto Warden": "ON",
    "Ottawa": "ON",
    "Leaside": "ON",
    "Barrie": "ON",
    "British Columbia": "BC",
    "Newfound Land": "NL",
}

ADMIN_NAMES = {"Jay Puzon", "Mandy Leong", "Dave Powell"}


def map_location_to_province(location: str) -> str:
    province = LOCATION_PROVINCE_MAP.get(location)
    if not province:
        raise ValueError(f"Province cannot be determined for location: {location}")
    return province


async def get_employee_by_name(name: str) -> dict | None:
    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/Title eq '{_escape_odata(name)}'",
        top=1,
    )
    if not items:
        logger.warning("Employee not found by name: %s", name)
        return None
    return items[0]


async def get_employee_by_email(email: str) -> dict | None:
    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/EmailAddress eq '{_escape_odata(email)}'",
        top=1,
    )
    if not items:
        logger.warning("Employee not found by email: %s", email)
        return None
    return items[0]


async def get_employee_by_id(item_id: str | int) -> dict | None:
    try:
        return await sp_client.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, item_id)
    except Exception:
        logger.warning("Employee not found by ID: %s", item_id)
        return None


async def get_all_managers_for_employee(employee: dict) -> list[dict]:
    """Get all managers from the AllManagers Person/Group field on Staff Directory.
    Falls back to the Supervisor text field if AllManagers is empty.
    """
    fields = employee.get("fields", {})
    all_managers_field = fields.get("AllManagers")

    managers = []
    if all_managers_field and isinstance(all_managers_field, list):
        for entry in all_managers_field:
            name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
            if name:
                mgr = await get_employee_by_name(name)
                if mgr:
                    managers.append(mgr)

    if managers:
        return managers

    # Fall back to Supervisor field
    supervisor = fields.get("Supervisor")
    if supervisor:
        mgr = await get_employee_by_name(supervisor)
        if mgr:
            return [mgr]

    logger.warning("No managers found for employee: %s", fields.get("Title"))
    return []


async def get_manager_for_employee(employee: dict) -> dict | None:
    managers = await get_all_managers_for_employee(employee)
    return managers[0] if managers else None


async def is_manager(employee_name: str) -> bool:
    """Check if anyone lists this employee as their Supervisor or in AllManagers."""
    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/Supervisor eq '{_escape_odata(employee_name)}'",
        top=1,
        select=["Title"],
    )
    if items:
        return True

    # AllManagers is Person/Group multi-value — not OData-filterable, scan client-side
    all_staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    for staff in all_staff:
        all_managers = staff.get("fields", {}).get("AllManagers")
        if all_managers and isinstance(all_managers, list):
            for entry in all_managers:
                name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
                if name == employee_name:
                    return True

    return False


async def get_employee_roles(employee: dict) -> list[str]:
    """Determine all dashboard roles for an employee."""
    fields = employee.get("fields", {})
    name = fields.get("Title", "")
    roles = ["employee"]
    if await is_manager(name):
        roles.append("manager")
    if name in ADMIN_NAMES:
        roles.append("admin")
    return roles


def _escape_odata(value: str) -> str:
    return value.replace("'", "''")
