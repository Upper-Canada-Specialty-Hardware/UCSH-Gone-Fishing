from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.base import ManagerAssignmentRepository


class SharePointManagerAssignmentRepository(ManagerAssignmentRepository):
    """Manager assignments backed by SharePoint. In SharePoint they live inline
    on the Staff Directory `AllManagers` person field, so this returns the raw
    Staff Directory items; services/manager_assignments.py extracts and resolves
    the AllManagers entries. The Postgres impl (later) will read the dedicated
    manager_assignments table instead."""

    _list_id = settings.SP_LIST_STAFF_DIRECTORY

    async def get_all_assignments(self) -> list[dict]:
        return await sp_client.get_list_items(self._list_id)
