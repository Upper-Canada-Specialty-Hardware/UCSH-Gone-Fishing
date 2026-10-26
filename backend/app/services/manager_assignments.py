import logging

from app.config import settings
from app.repositories.staff_store import staff_store, staff_in_postgres
from app.services.employee import _get_sp_user_name_map

logger = logging.getLogger(__name__)


async def _get_sp_name_to_id_map() -> dict[str, int]:
    """Build name → SP user ID map from User Information List."""
    sp_user_map = await _get_sp_user_name_map()
    return {name: uid for uid, name in sp_user_map.items()}


async def _manager_id_to_name() -> dict[int, str]:
    """Map a manager identifier to a display name, from whichever backend holds staff.

    On Postgres there is no SharePoint User Information List, so a manager is
    identified by their own staff id and the name comes from the staff table
    (#265). On SharePoint the identifier is the Microsoft 365 user id and the
    name comes from the user list, exactly as before.

    Returns:
        ``{identifier: display name}`` for the managers the screen can name.
    """
    if await staff_in_postgres():
        out: dict[int, str] = {}
        # Archived people are kept (#119): a request may still name an archived
        # manager, and the screens need that id to resolve to their name.
        for item in await staff_store.get_list_items(
            settings.SP_LIST_STAFF_DIRECTORY, include_archived=True
        ):
            try:
                out[int(item["id"])] = item.get("fields", {}).get("Title", "")  # staff id -> name
            except (TypeError, ValueError):
                continue                                       # a non-numeric id is not a manager key
        return out
    return await _get_sp_user_name_map()


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
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    # The id -> name map comes from the staff table on Postgres (no fallback to the
    # SharePoint user list after the switch) and from the user list otherwise (#265).
    sp_user_map = await _manager_id_to_name()

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
    """Return all staff members with their manager identifier for autocomplete."""
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    # On Postgres there is no SharePoint user list; the person's own staff id is
    # the manager identifier the assignment screen passes back, so no name->id
    # map is needed and everyone is selectable (#265).
    postgres = await staff_in_postgres()
    name_to_id = {} if postgres else await _get_sp_name_to_id_map()

    users = []
    for item in items:
        fields = item.get("fields", {})
        name = fields.get("Title", "")
        if not name:
            continue
        sp_user_id = int(item["id"]) if postgres else name_to_id.get(name)
        if not sp_user_id:
            continue                                           # SharePoint: not on the user list, not selectable
        users.append({
            "staff_id": str(item["id"]),
            "sp_user_id": sp_user_id,
            "name": name,
            "email": fields.get("EmailAddress", ""),
            "department": fields.get("Department", ""),
            "location": fields.get("Location", ""),
        })

    return users


async def update_employee_managers(
    employee_id: int, manager_sp_user_ids: list[int],
    history_source: str = "system",
    history_reason: str = "Updated from the manager assignment screen.",
    history_actor: str | None = None,
) -> dict:
    """Update the AllManagers field on a single Staff Directory record.

    Routed through ``staff_store``, so the change lands in Postgres when staff live
    there (recording one history row with ``history_source``/``history_reason``) and
    goes straight to SharePoint otherwise, byte for byte as before.

    Args:
        employee_id: Staff Directory item id of the person whose managers change.
        manager_sp_user_ids: The SharePoint user ids of the approving managers; an
            empty list clears them.
        history_source: Which channel to record the AllManagers change under in the
            history; ``"system"`` for the admin assignment screen (the default),
            ``"manager_dashboard"`` when a manager adds or removes themselves (#363).
        history_reason: The reason stored with that history row, naming who made the
            change for a manager-dashboard edit.
        history_actor: Who made the change (the manager's name for a manager-dashboard
            edit); None leaves the reader to fall back to ``history_source`` (#447).

    Returns:
        ``{"id", "name", "managers"}`` with the managers resolved to names.
    """
    # The write shape carries the manager ids; resolve their names once, up
    # front, so the Postgres store stores the names too and the return reads as
    # names. The map comes from the staff table when staff live in Postgres (no
    # SharePoint user list there) and from the user list otherwise (#265).
    sp_user_map = await _manager_id_to_name()
    # Always send the collection type plus the id list; an empty list clears them.
    update_fields: dict = {
        "AllManagersLookupId@odata.type": "Collection(Edm.Int32)",
        "AllManagersLookupId": manager_sp_user_ids or [],
    }
    # The same managers with names, for the Postgres backend (ignored by SharePoint).
    links = {
        "managers": [
            {"sp_user_id": mid, "name": sp_user_map.get(mid, "")}
            for mid in (manager_sp_user_ids or [])
        ]
    }

    await staff_store.update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, employee_id, update_fields,
        links=links,
        history_source=history_source,                   # "system", or a manager's own team (#363)
        history_reason=history_reason,
        history_actor=history_actor,                     # the manager's name for a team edit (#447)
    )
    logger.info("Updated AllManagers for employee %s: %s", employee_id, manager_sp_user_ids)

    # Re-read and return updated data (through the store, so it follows the backend).
    item = await staff_store.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, employee_id)
    fields = item.get("fields", {})
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
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    # Postgres: names from the staff table, never the SharePoint user list (#265).
    sp_user_map = await _manager_id_to_name()

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
