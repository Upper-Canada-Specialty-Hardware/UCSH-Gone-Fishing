import logging
from urllib.parse import quote

import httpx

from app.config import settings
from app.graph.client import graph_client

logger = logging.getLogger(__name__)

# The most pages a single list read will fetch before it gives up. Set high so a
# real list is always read whole; a read that still hits it is treated as
# truncated and raised, never quietly returned short (#336 fix).
MAX_PAGES = 1000


class SharePointReadTruncated(RuntimeError):
    """A list read hit the page safety limit, so it may be missing items.

    Raised instead of returning a short list, so the sync fails and the switch
    refuses rather than treating unread items as deleted.
    """


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

        for page in range(MAX_PAGES):
            data = await graph_client.get(url, params=params)
            items.extend(data.get("value", []))

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

            # nextLink is an absolute URL with query params embedded
            url = next_link
            params = None
        else:
            # Still a next page after MAX_PAGES: the read is incomplete, so the
            # items we have are not the whole list. Raise rather than return short,
            # so the caller (the sync) fails instead of taking unread items as gone.
            logger.error(
                "get_list_items hit %d-page safety limit for list %s (fetched %d items)",
                MAX_PAGES, list_id, len(items),
            )
            raise SharePointReadTruncated(
                f"list {list_id} read stopped after {MAX_PAGES} pages ({len(items)} items)"
            )

        return items

    async def get_list_item(self, list_id: str, item_id: str | int) -> dict:
        path = f"{self._list_path(list_id)}/items/{item_id}"
        params = {"$expand": "fields"}
        return await graph_client.get(path, params=params)

    async def get_list_item_or_none(self, list_id: str, item_id: str | int) -> dict | None:
        """get_list_item, but a deleted item is None instead of an exception.

        Only 404 maps to None. Every other status still raises, so throttling, an
        outage, or a permissions change is never mistaken for a deleted item.
        Callers that treat "missing" as a terminal state (closing a reminder row,
        rejecting an admin action) should use this rather than catching
        httpx.HTTPStatusError themselves.
        """
        try:
            return await self.get_list_item(list_id, item_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def create_list_item(self, list_id: str, fields: dict) -> dict:
        # Rehearsal mode must not change SharePoint; reads stay open, writes skip
        # with a log line and a harmless empty result (#339).
        from app.services.rehearsal import sharepoint_writes_allowed
        if not sharepoint_writes_allowed():
            logger.warning("Rehearsal: skipped creating an item on list %s", list_id)
            return {}
        path = f"{self._list_path(list_id)}/items"
        return await graph_client.post(path, json={"fields": fields})

    async def update_list_item_fields(
        self, list_id: str, item_id: str | int, fields: dict
    ) -> dict:
        from app.services.rehearsal import sharepoint_writes_allowed
        if not sharepoint_writes_allowed():
            logger.warning("Rehearsal: skipped updating item %s on list %s", item_id, list_id)
            return {}
        path = f"{self._list_path(list_id)}/items/{item_id}/fields"
        return await graph_client.patch(path, json=fields)

    async def delete_list_item(self, list_id: str, item_id: str | int) -> None:
        """Permanently delete a list item via Graph (204 on success).

        Args:
            list_id: The SharePoint list the item lives in.
            item_id: The item to delete.
        """
        from app.services.rehearsal import sharepoint_writes_allowed
        if not sharepoint_writes_allowed():
            logger.warning("Rehearsal: skipped deleting item %s on list %s", item_id, list_id)
            return
        path = f"{self._list_path(list_id)}/items/{item_id}"
        await graph_client.delete(path)  # Graph returns 204 No Content

    async def get_delta(self, list_id: str, token: str | None = None) -> dict:
        """Fetch all pages of a delta query, returning combined items + deltaLink."""
        if token:
            url = f"{self._list_path(list_id)}/items/delta?token={quote(token)}"
        else:
            url = f"{self._list_path(list_id)}/items/delta"

        all_items: list[dict] = []
        delta_link = ""
        params = None

        for page in range(MAX_PAGES):
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
            # No deltaLink after MAX_PAGES: the delta is incomplete, so raise rather
            # than hand back a short page with no delta token to resume from.
            logger.error(
                "get_delta hit %d-page safety limit for list %s (fetched %d items)",
                MAX_PAGES, list_id, len(all_items),
            )
            raise SharePointReadTruncated(
                f"delta for list {list_id} stopped after {MAX_PAGES} pages ({len(all_items)} items)"
            )

        return {"value": all_items, "@odata.deltaLink": delta_link}

    async def close(self):
        await graph_client.close()


sp_client = SharePointClient()
