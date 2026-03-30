import logging

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.employee import _get_sp_user_name_map

logger = logging.getLogger(__name__)


async def _get_sp_name_to_id_map() -> dict[str, int]:
    """Build name → SP user ID map from User Information List."""
    sp_user_map = await _get_sp_user_name_map()
    return {name: uid for uid, name in sp_user_map.items()}


def _extract_all_managers(fields: dict) -> list[dict]:
    """Extract AllManagers entries as [{sp_user_id, name}]."""
    result = []
    all_managers = fields.get("AllManagers")
    if all_managers and isinstance(all_managers, list):
        for entry in all_managers:
            if isinstance(entry, dict):
                lookup_id = entry.get("LookupId")
                name = entry.get("LookupValue", "")
                if lookup_id:
                    result.append({"sp_user_id": int(lookup_id), "name": name})
    return result


async def get_all_assignments() -> list[dict]:
    """Fetch all Staff Directory employees with their resolved AllManagers."""
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    sp_user_map = await _get_sp_user_name_map()

    assignments = []
    for item in items:
        fields = item.get("fields", {})
        name = fields.get("Title", "")
        if not name:
            continue

        managers = _extract_all_managers(fields)
        # Resolve names for managers whose LookupValue is empty
        for mgr in managers:
            if not mgr["name"] and mgr["sp_user_id"] in sp_user_map:
                mgr["name"] = sp_user_map[mgr["sp_user_id"]]

        assignments.append({
            "id": str(item["id"]),
            "name": name,
            "email": fields.get("EmailAddress", ""),
            "department": fields.get("Department", ""),
            "location": fields.get("Location", ""),
            "managers": managers,
        })

    return assignments


async def get_staff_as_sp_users() -> list[dict]:
    """Return all staff members with their SP user IDs for autocomplete."""
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    name_to_id = await _get_sp_name_to_id_map()

    users = []
    for item in items:
        fields = item.get("fields", {})
        name = fields.get("Title", "")
        if not name:
            continue
        sp_user_id = name_to_id.get(name)
        if not sp_user_id:
            continue
        users.append({
            "staff_id": str(item["id"]),
            "sp_user_id": sp_user_id,
            "name": name,
            "email": fields.get("EmailAddress", ""),
            "department": fields.get("Department", ""),
            "location": fields.get("Location", ""),
        })

    return users


async def update_employee_managers(employee_id: int, manager_sp_user_ids: list[int]) -> dict:
    """Update the AllManagers field on a single Staff Directory record."""
    update_fields: dict = {}
    if manager_sp_user_ids:
        update_fields["AllManagersLookupId@odata.type"] = "Collection(Edm.Int32)"
        update_fields["AllManagersLookupId"] = manager_sp_user_ids
    else:
        # Clear AllManagers by setting to empty array
        update_fields["AllManagersLookupId@odata.type"] = "Collection(Edm.Int32)"
        update_fields["AllManagersLookupId"] = []

    await sp_client.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, employee_id, update_fields
    )
    logger.info("Updated AllManagers for employee %s: %s", employee_id, manager_sp_user_ids)

    # Re-read and return updated data
    item = await sp_client.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, employee_id)
    fields = item.get("fields", {})
    sp_user_map = await _get_sp_user_name_map()
    managers = _extract_all_managers(fields)
    for mgr in managers:
        if not mgr["name"] and mgr["sp_user_id"] in sp_user_map:
            mgr["name"] = sp_user_map[mgr["sp_user_id"]]

    return {
        "id": str(employee_id),
        "name": fields.get("Title", ""),
        "managers": managers,
    }


async def preview_bulk_operation(operation: str, params: dict) -> dict:
    """Dry-run a bulk operation and return what would change."""
    items = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    sp_user_map = await _get_sp_user_name_map()

    source_id = params.get("source_manager_id")
    target_id = params.get("target_manager_id")

    affected = []
    for item in items:
        fields = item.get("fields", {})
        name = fields.get("Title", "")
        if not name:
            continue
        current_managers = _extract_all_managers(fields)
        current_ids = [m["sp_user_id"] for m in current_managers]

        new_ids = None
        if operation == "replace" and source_id and target_id:
            if source_id in current_ids:
                new_ids = [target_id if mid == source_id else mid for mid in current_ids]
        elif operation == "add" and target_id:
            employee_ids = params.get("employee_ids")
            if employee_ids:
                if str(item["id"]) in [str(eid) for eid in employee_ids] and target_id not in current_ids:
                    new_ids = current_ids + [target_id]
            elif source_id:
                if source_id in current_ids and target_id not in current_ids:
                    new_ids = current_ids + [target_id]
        elif operation == "remove" and source_id:
            if source_id in current_ids:
                new_ids = [mid for mid in current_ids if mid != source_id]

        if new_ids is not None:
            # Resolve names
            current_names = []
            for mgr in current_managers:
                n = mgr["name"] or sp_user_map.get(mgr["sp_user_id"], "")
                current_names.append(n)
            new_names = [sp_user_map.get(mid, f"ID:{mid}") for mid in new_ids]

            affected.append({
                "id": str(item["id"]),
                "name": name,
                "department": fields.get("Department", ""),
                "current_managers": current_names,
                "new_managers": new_names,
                "new_manager_ids": new_ids,
            })

    return {"affected_count": len(affected), "affected_employees": affected}


async def execute_bulk_operation(operation: str, params: dict) -> dict:
    """Execute a bulk operation and return results."""
    preview = await preview_bulk_operation(operation, params)
    affected = preview["affected_employees"]

    results = []
    for emp in affected:
        try:
            await update_employee_managers(int(emp["id"]), emp["new_manager_ids"])
            results.append({"id": emp["id"], "name": emp["name"], "status": "success"})
        except Exception as e:
            logger.exception("Failed to update managers for employee %s", emp["id"])
            results.append({"id": emp["id"], "name": emp["name"], "status": "error", "detail": str(e)})

    success_count = sum(1 for r in results if r["status"] == "success")
    return {"total": len(results), "success": success_count, "results": results}
