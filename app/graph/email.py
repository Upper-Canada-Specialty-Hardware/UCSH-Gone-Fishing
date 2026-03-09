import logging

from app.config import settings
from app.graph.client import graph_client

logger = logging.getLogger(__name__)


async def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    importance: str = "Normal",
    attachments: list[dict] | None = None,
):
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
        "importance": importance,
    }

    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc]

    if attachments:
        message["attachments"] = attachments

    path = f"/users/{settings.SENDER_EMAIL}/sendMail"
    await graph_client.post(path, json={"message": message, "saveToSentItems": False})
    logger.info("Email sent to %s — subject: %s", to, subject)
