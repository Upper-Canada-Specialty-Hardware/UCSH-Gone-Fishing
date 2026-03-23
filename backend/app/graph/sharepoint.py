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

        url = f"{self._list_path(list_id)}/items"
        items: list[dict] = []
        max_pages = 50

        for page in range(max_pages):
            data = await graph_client.get(url, params=params)
            items.extend(data.get("value", []))

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

            # nextLink is an absolute URL with query params embedded
            url = next_link
            params = None
        else:
            logger.warning(
                "get_list_items hit %d-page safety limit for list %s (fetched %d items)",
                max_pages, list_id, len(items),
            )

        return items

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
        """Fetch all pages of a delta query, returning combined items + deltaLink."""
        if token:
            url = f"{self._list_path(list_id)}/items/delta?token={quote(token)}"
        else:
            url = f"{self._list_path(list_id)}/items/delta"

        all_items: list[dict] = []
        delta_link = ""
        params = None
        max_pages = 50

        for page in range(max_pages):
            data = await graph_client.get(url, params=params)
            all_items.extend(data.get("value", []))

            # deltaLink only appears on the final page
            if "@odata.deltaLink" in data:
                delta_link = data["@odata.deltaLink"]
                break

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

            url = next_link
            params = None
        else:
            logger.warning(
                "get_delta hit %d-page safety limit for list %s (fetched %d items)",
                max_pages, list_id, len(all_items),
            )

        return {"value": all_items, "@odata.deltaLink": delta_link}

    async def close(self):
        await graph_client.close()


sp_client = SharePointClient()
