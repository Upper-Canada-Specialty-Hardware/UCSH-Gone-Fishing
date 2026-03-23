import logging
import secrets
from datetime import datetime, timedelta

from app.config import settings
from app.graph.client import graph_client
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)


async def create_subscription(list_id: str) -> dict:
    client_state = secrets.token_hex(16)
    expiration = datetime.utcnow() + timedelta(days=29)
    path = f"/sites/{sp_client.site_id}/lists/{list_id}/subscriptions"
    body = {
        "changeType": "updated,created",
        "notificationUrl": f"{settings.BASE_URL}/api/webhooks/sharepoint",
        "expirationDateTime": expiration.isoformat() + "Z",
        "clientState": client_state,
    }
    data = await graph_client.post(path, json=body)
    logger.info("Created webhook subscription %s for list %s", data.get("id"), list_id)
    return {
        "id": data["id"],
        "list_id": list_id,
        "expiration": expiration,
        "client_state": client_state,
    }


async def renew_subscription(subscription_id: str, list_id: str) -> datetime:
    expiration = datetime.utcnow() + timedelta(days=29)
    path = f"/sites/{sp_client.site_id}/lists/{list_id}/subscriptions/{subscription_id}"
    await graph_client.patch(path, json={"expirationDateTime": expiration.isoformat() + "Z"})
    logger.info("Renewed subscription %s, new expiration: %s", subscription_id, expiration)
    return expiration


async def delete_subscription(subscription_id: str, list_id: str) -> None:
    path = f"/sites/{sp_client.site_id}/lists/{list_id}/subscriptions/{subscription_id}"
    await graph_client.delete(path)
    logger.info("Deleted subscription %s", subscription_id)
