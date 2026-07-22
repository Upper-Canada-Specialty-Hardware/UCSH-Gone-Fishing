from app.graph.sharepoint import sp_client
from app.repositories.base import RequestRepository


class SharePointRequestRepository(RequestRepository):
    """A SharePoint-list-backed request repo. One instance per request list
    (leave / overtime / carryover-payout); the only difference is the list id.

    get_by_id lets Graph errors propagate (it does not swallow them), matching
    the current direct sp_client.get_list_item calls where callers such as the
    dispatcher and the Twilio route handle their own exceptions.
    """

    def __init__(self, list_id: str):
        self._list_id = list_id

    async def get_all(self) -> list[dict]:
        return await sp_client.get_list_items(self._list_id)

    async def get_by_id(self, item_id: str | int) -> dict:
        return await sp_client.get_list_item(self._list_id, item_id)

    async def create(self, fields: dict) -> dict:
        return await sp_client.create_list_item(self._list_id, fields)

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        return await sp_client.update_list_item_fields(self._list_id, item_id, fields)
