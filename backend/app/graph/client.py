import asyncio
import logging

import httpx

from app.graph.auth import token_manager

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphClient:
    def __init__(self):
        self._http = httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30.0)

    async def _headers(self) -> dict:
        token = await token_manager.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get(self, path: str, params: dict | None = None, *, allow_after_switch: bool = False) -> dict:
        resp = await self._request("GET", path, params=params, allow_after_switch=allow_after_switch)
        return resp.json()

    async def post(self, path: str, json: dict | None = None) -> dict:
        resp = await self._request("POST", path, json=json)
        if resp.status_code in (202, 204) or not resp.content:
            return {}
        return resp.json()

    async def patch(self, path: str, json: dict | None = None) -> dict:
        resp = await self._request("PATCH", path, json=json)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

    async def _request(self, method: str, path: str, *, allow_after_switch: bool = False, **kwargs) -> httpx.Response:
        """Send one Graph call, refusing it once the switch to Postgres has run.

        Every SharePoint read and write passes through here, so this one check
        keeps the whole app off SharePoint after the switch (no fallbacks). The
        one exception is the staff-history import (#448): with
        ``allow_after_switch`` set, a GET the import is allowed to make (a Staff
        Directory versions read, or the site-id lookup it needs) skips the gate;
        every other call, and every write, is still refused.

        Args:
            method: HTTP method.
            path: Graph path (relative, or an absolute ``@odata.nextLink``).
            allow_after_switch: Opt-in for the staff-history import's reads only.
            **kwargs: Passed through to the HTTP send (params/json).

        Raises:
            SharePointCutOff: After the switch; no token is fetched, nothing is sent.
        """
        from app.services.sharepoint_cutoff import refuse_if_cut_off, is_staff_history_read  # lazy: avoid a cycle
        if not (allow_after_switch and is_staff_history_read(method, path)):   # only the import's own reads skip the gate
            await refuse_if_cut_off(method, path)              # before the token, so nothing leaves the app
        headers = await self._headers()
        resp = await self._http.request(method, path, headers=headers, **kwargs)

        # Auto-retry on 401 (token expired mid-request)
        if resp.status_code == 401:
            logger.warning("Got 401, refreshing token and retrying...")
            await token_manager._acquire_token()
            headers = await self._headers()
            resp = await self._http.request(method, path, headers=headers, **kwargs)

        # Retry on 429 (throttled) — respect Retry-After header
        retries = 0
        while resp.status_code == 429 and retries < 3:
            retry_after = int(resp.headers.get("Retry-After", 5))
            retries += 1
            logger.warning("Graph API throttled (429), retry %d/3 after %ds — %s %s", retries, retry_after, method, path)
            await asyncio.sleep(retry_after)
            headers = await self._headers()
            resp = await self._http.request(method, path, headers=headers, **kwargs)

        if resp.status_code >= 400:
            logger.error(
                "Graph API %s %s → %d: %s",
                method, path, resp.status_code, resp.text[:500],
            )
        resp.raise_for_status()
        return resp

    async def close(self):
        await self._http.aclose()


graph_client = GraphClient()
