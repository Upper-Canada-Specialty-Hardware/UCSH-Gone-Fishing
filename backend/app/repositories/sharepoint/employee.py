from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.base import EmployeeRepository


class SharePointEmployeeRepository(EmployeeRepository):
    """Staff Directory backed by SharePoint (today's source of truth).

    Title/EmailAddress are not indexed on the list, so name/email lookups fetch
    all items and filter client-side — the existing behavior from
    services/employee.py, moved behind the interface. get_by_id swallows errors
    to return None, matching the current get_employee_by_id.
    """

    _list_id = settings.SP_LIST_STAFF_DIRECTORY

    async def get_all(self) -> list[dict]:
        return await sp_client.get_list_items(self._list_id)

    async def get_by_id(self, item_id: str | int) -> dict | None:
        try:
            return await sp_client.get_list_item(self._list_id, int(item_id))
        except Exception:
            return None

    async def get_by_name(self, name: str) -> dict | None:
        target = name.strip().lower()
        for item in await self.get_all():
            if item.get("fields", {}).get("Title", "").strip().lower() == target:
                return item
        return None

    async def get_by_email(self, email: str) -> dict | None:
        target = email.strip().lower()
        for item in await self.get_all():
            if item.get("fields", {}).get("EmailAddress", "").strip().lower() == target:
                return item
        return None

    async def create(self, fields: dict) -> dict:
        """Insert a new Staff Directory list item via Graph and return it.

        Args:
            fields: SharePoint column names -> values (Title, EmailAddress,
                balances, entitlements) — the payload build_employee_fields
                assembles. Person/Group columns (AllManagers) are written
                separately by the caller, not here.

        Returns:
            The created item as {"id": <str>, "fields": {...}}, identical to
            what get_by_id returns for the same record.
        """
        return await sp_client.create_list_item(self._list_id, fields)  # thin Graph pass-through, shape unchanged

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        return await sp_client.update_list_item_fields(self._list_id, item_id, fields)
