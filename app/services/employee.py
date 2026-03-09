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


async def get_manager_for_employee(employee: dict) -> dict | None:
    fields = employee.get("fields", {})
    supervisor = fields.get("Supervisor")
    if not supervisor:
        logger.warning("No supervisor set for employee: %s", fields.get("Title"))
        return None
    return await get_employee_by_name(supervisor)


def _escape_odata(value: str) -> str:
    return value.replace("'", "''")
