import logging

from app.graph.sharepoint import sp_client
from app.repositories import get_employee_repository

logger = logging.getLogger(__name__)

# sp_client is still imported for _get_sp_user_name_map below: the M365 User
# Information List is *identity*, which stays in SharePoint after the
# migration, so it is deliberately not behind the repository seam.

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
    employee = await get_employee_repository().get_by_name(name)
    if employee is None:
        logger.warning("Employee not found by name: %s", name)
    return employee


async def get_employee_by_email(email: str) -> dict | None:
    employee = await get_employee_repository().get_by_email(email)
    if employee is None:
        logger.warning("Employee not found by email: %s", email)
    return employee


async def get_employee_by_id(item_id: str | int) -> dict | None:
    employee = await get_employee_repository().get_by_id(item_id)
    if employee is None:
        logger.warning("Employee not found by ID: %s", item_id)
    return employee


async def get_all_managers_for_employee(employee: dict) -> list[dict]:
    """Get all managers from the AllManagers Person/Group field on Staff Directory."""
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

    if not managers:
        logger.warning("No managers found for employee: %s", fields.get("Title"))
    return managers


async def get_manager_for_employee(employee: dict) -> dict | None:
    managers = await get_all_managers_for_employee(employee)
    return managers[0] if managers else None


async def is_manager(employee_name: str) -> bool:
    """Check if anyone lists this employee in their AllManagers field."""
    all_staff = await get_employee_repository().get_all()
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


async def _get_sp_user_name_map() -> dict[int, str]:
    """Fetch SP User Information List → {sp_user_id: display_name}."""
    result: dict[int, str] = {}
    try:
        user_items = await sp_client.get_list_items("User Information List", top=5000)
        for u in user_items:
            uid = u.get("id")
            uname = u.get("fields", {}).get("Title", "")
            if uid and uname:
                result[int(uid)] = uname
    except Exception:
        logger.exception("Failed to fetch User Information List")
    return result


async def resolve_person_field(person_field) -> dict | None:
    """Resolve a SP Person/Group field to a Staff Directory employee record.

    The Graph API only returns LookupId for Person/Group columns (LookupValue
    is always empty). This function maps LookupId → display name via the
    User Information List, then looks up the employee in Staff Directory.

    Accepts either a dict with a LookupId key (complex object form) or a raw
    LookupId value (int/str) since Graph API often only returns the *LookupId
    suffixed field (e.g. SubmittedTestLookupId) without the complex object.
    """
    if not person_field:
        return None
    if isinstance(person_field, dict):
        lookup_id = person_field.get("LookupId")
    else:
        lookup_id = person_field
    if not lookup_id:
        return None
    sp_user_map = await _get_sp_user_name_map()
    try:
        display_name = sp_user_map.get(int(lookup_id), "")
    except (ValueError, TypeError):
        return None
    if not display_name:
        logger.warning("SP user ID %s not found in User Information List", lookup_id)
        return None
    return await get_employee_by_name(display_name)


async def resolve_person_field_name(person_field) -> str:
    """Resolve a SP Person/Group field to a display name only.

    Accepts either a dict with LookupId or a raw LookupId value.
    """
    if not person_field:
        return ""
    if isinstance(person_field, dict):
        lookup_id = person_field.get("LookupId")
    else:
        lookup_id = person_field
    if not lookup_id:
        return ""
    sp_user_map = await _get_sp_user_name_map()
    try:
        return sp_user_map.get(int(lookup_id), "")
    except (ValueError, TypeError):
        return ""
