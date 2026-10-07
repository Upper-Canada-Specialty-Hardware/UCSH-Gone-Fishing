from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.base import HolidayRepository


class SharePointHolidayRepository(HolidayRepository):
    """Company Holidays backed by SharePoint. Province is not indexed, so the
    service fetches all rows and filters client-side; this repo just returns
    the raw list items."""

    _list_id = settings.SP_LIST_COMPANY_HOLIDAYS

    async def get_all(self) -> list[dict]:
        return await sp_client.get_list_items(self._list_id)

    async def get_by_id(self, item_id: str | int) -> dict | None:
        try:
            return await sp_client.get_list_item(self._list_id, int(item_id))
        except Exception:
            return None

    async def create(self, fields: dict) -> dict:
        """Insert a Company Holidays list item via Graph.

        Args:
            fields: SharePoint column names -> values (Title, Date, Province).

        Returns:
            The created item as {"id","fields"}.
        """
        return await sp_client.create_list_item(self._list_id, fields)  # thin Graph pass-through

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Patch a Company Holidays item's columns via Graph.

        Args:
            item_id: The item to patch.
            fields: SharePoint column names -> new values.

        Returns:
            The updated item as {"id","fields"} (re-read after the patch).
        """
        await sp_client.update_list_item_fields(self._list_id, item_id, fields)  # apply the patch
        return await sp_client.get_list_item(self._list_id, int(item_id))  # return the full updated shape

    async def delete(self, item_id: str | int) -> None:
        """Delete a Company Holidays item via Graph.

        Args:
            item_id: The item to remove.

        Raises:
            KeyError: If the item does not exist (mirrors the Postgres impl).
        """
        if await self.get_by_id(item_id) is None:  # normalise Graph's 404 into KeyError
            raise KeyError(f"No holiday with id {item_id}")
        await sp_client.delete_list_item(self._list_id, item_id)
