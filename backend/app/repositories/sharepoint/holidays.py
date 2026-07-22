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
