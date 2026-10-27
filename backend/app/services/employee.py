import logging

from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.staff_store import staff_store

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
    # Title is not indexed on Staff Directory, so fetch all and match in memory.
    # Routed through staff_store so the read follows staff_storage (SharePoint or Postgres).
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    target = name.strip().lower()
    for item in items:
        if item.get("fields", {}).get("Title", "").strip().lower() == target:
            return item
    logger.warning("Employee not found by name: %s", name)
    return None


async def get_employee_by_email(email: str) -> dict | None:
    # EmailAddress is not indexed on Staff Directory, so fetch all and match in memory.
    # Routed through staff_store so the read follows staff_storage (SharePoint or Postgres).
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    target = email.strip().lower()
    for item in items:
        if item.get("fields", {}).get("EmailAddress", "").strip().lower() == target:
            return item
    logger.warning("Employee not found by email: %s", email)
    return None


async def get_employees_by_email(email: str) -> list[dict]:
    """Every staff record carrying an email, in Staff Directory order.

    One mailbox can genuinely belong to two records (a shared desk such as
    Accounts Payable and the person who reads it), so sign-in asks which one.

    Args:
        email: The address, matched case-insensitively.

    Returns:
        The matching records (``{"id", "fields"}``); empty when none match.
    """
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)   # one read of the directory
    target = email.strip().lower()
    return [
        item for item in items
        if (item.get("fields", {}).get("EmailAddress") or "").strip().lower() == target
    ]


async def get_employee_by_id(item_id: str | int) -> dict | None:
    try:
        # Routed through staff_store so the read follows staff_storage (SharePoint or Postgres).
        # A single-id lookup is never filtered, so an archived person still resolves (#119).
        return await staff_store.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, int(item_id))
    except Exception:
        logger.warning("Employee not found by ID: %s", item_id)
        return None


async def email_belongs_to_archived(email: str) -> bool:
    """Whether every staff record on this email is archived (#119).

    The default directory read leaves archived people out, so ``get_employees_by_email``
    cannot see them; this read keeps them, to tell "archived, refuse" apart from
    "not on staff, ask your manager". A shared mailbox with even one active record is
    not treated as archived, so that record still signs in.

    Args:
        email: The address, matched case-insensitively.

    Returns:
        True when at least one record carries the address and all of them are
        archived; False when none match or any active record carries it.
    """
    from app.repositories.postgres.staff import is_archived   # local: avoids importing it app-wide
    from app.repositories.staff_store import staff_in_postgres
    if not await staff_in_postgres():                       # no archive concept on SharePoint
        return False
    items = await staff_store.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY, include_archived=True
    )
    target = email.strip().lower()
    matched = [
        item for item in items
        if (item.get("fields", {}).get("EmailAddress") or "").strip().lower() == target
    ]
    if not matched:                                          # nobody on this address at all
        return False
    return all(is_archived(item.get("fields", {})) for item in matched)


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
    """Whether anyone lists this person in their AllManagers field (#376).

    Args:
        employee_name: Their name; a ``ManagerName`` (``manager_match``) also
            matches by their link id, so a manager whose Microsoft 365 name
            differs from their staff name is still found.

    Returns:
        True when at least one person lists them.
    """
    from app.services.manager_match import lists_manager   # lazy: manager_match imports staff_store
    # Routed through staff_store so the read follows staff_storage (SharePoint or Postgres).
    all_staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    return any(lists_manager(staff.get("fields", {}), employee_name) for staff in all_staff)


async def get_employee_roles(employee: dict) -> list[str]:
    """Determine all dashboard roles for an employee."""
    fields = employee.get("fields", {})
    name = fields.get("Title", "")
    roles = ["employee"]
    from app.services.manager_match import manager_name_of   # lazy, as in is_manager
    if await is_manager(await manager_name_of(employee)):     # by id or by name (#376)
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
