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

    async def get(self, path: str, params: dict | None = None) -> dict:
        resp = await self._request("GET", path, params=params)
        return resp.json()

    async def post(self, path: str, json: dict | None = None) -> dict:
        resp = await self._request("POST", path, json=json)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def patch(self, path: str, json: dict | None = None) -> dict:
        resp = await self._request("PATCH", path, json=json)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = await self._headers()
        resp = await self._http.request(method, path, headers=headers, **kwargs)

        # Auto-retry on 401 (token expired mid-request)
        if resp.status_code == 401:
            logger.warning("Got 401, refreshing token and retrying...")
            await token_manager._acquire_token()
            headers = await self._headers()
            resp = await self._http.request(method, path, headers=headers, **kwargs)

        resp.raise_for_status()
        return resp

    async def close(self):
        await self._http.aclose()


graph_client = GraphClient()
