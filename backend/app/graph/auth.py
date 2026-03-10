import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TOKEN_URL = f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}/oauth2/v2.0/token"
REFRESH_BUFFER_SECONDS = 300  # Refresh when <5 min remaining


class TokenManager:
    def __init__(self):
        self._token: str | None = None
        self._expires_at: float = 0
        self._http = httpx.AsyncClient()

    async def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - REFRESH_BUFFER_SECONDS:
            return self._token
        return await self._acquire_token()

    async def _acquire_token(self) -> str:
        logger.info("Acquiring Graph API token...")
        resp = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": settings.AZURE_CLIENT_ID,
                "client_secret": settings.AZURE_CLIENT_SECRET,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data["expires_in"]
        logger.info("Graph API token acquired, expires in %ds", data["expires_in"])
        return self._token

    @property
    def is_valid(self) -> bool:
        return self._token is not None and time.time() < self._expires_at

    @property
    def seconds_until_expiry(self) -> float:
        return max(0, self._expires_at - time.time())

    async def close(self):
        await self._http.aclose()


token_manager = TokenManager()
