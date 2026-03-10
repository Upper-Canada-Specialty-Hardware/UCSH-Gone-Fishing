import logging
from urllib.parse import quote

from app.config import settings
from app.graph.client import graph_client

logger = logging.getLogger(__name__)


class SharePointClient:
    def __init__(self):
        self.site_id: str | None = None

    async def resolve_site_id(self):
        path = f"/sites/{settings.SP_SITE_HOST}:{settings.SP_SITE_PATH}"
        data = await graph_client.get(path)
        self.site_id = data["id"]
        logger.info("Resolved site ID: %s", self.site_id)

    def _list_path(self, list_id: str) -> str:
        return f"/sites/{self.site_id}/lists/{list_id}"

    async def get_list_items(
        self,
        list_id: str,
        filter: str | None = None,
        select: list[str] | None = None,
        expand: str = "fields",
        top: int = 5000,
    ) -> list[dict]:
        params = {"$expand": expand, "$top": str(top)}
        if filter:
            params["$filter"] = filter
        if select:
            params["$expand"] = f"fields($select={','.join(select)})"

        data = await graph_client.get(f"{self._list_path(list_id)}/items", params=params)
        return data.get("value", [])

    async def get_list_item(self, list_id: str, item_id: str | int) -> dict:
        path = f"{self._list_path(list_id)}/items/{item_id}"
        params = {"$expand": "fields"}
        return await graph_client.get(path, params=params)

    async def create_list_item(self, list_id: str, fields: dict) -> dict:
        path = f"{self._list_path(list_id)}/items"
        return await graph_client.post(path, json={"fields": fields})

    async def update_list_item_fields(
        self, list_id: str, item_id: str | int, fields: dict
    ) -> dict:
        path = f"{self._list_path(list_id)}/items/{item_id}/fields"
        return await graph_client.patch(path, json=fields)

    async def get_delta(self, list_id: str, token: str | None = None) -> dict:
        if token:
            path = f"{self._list_path(list_id)}/items/delta?token={quote(token)}"
        else:
            path = f"{self._list_path(list_id)}/items/delta"
        return await graph_client.get(path)

    async def close(self):
        await graph_client.close()


sp_client = SharePointClient()
