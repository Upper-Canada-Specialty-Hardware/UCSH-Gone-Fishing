import logging

from app.config import settings
from app.graph.client import graph_client

logger = logging.getLogger(__name__)


async def send_email_with_dashboard(
    to: list[str],
    subject: str,
    html_body: str,
    primary_employee_id: str | int | None = None,
    **kwargs,
):
    """Send email and automatically append dashboard footer for the primary recipient."""
    footer = ""
    if primary_employee_id and settings.DASHBOARD_FRONTEND_URL:
        try:
            from app.services.dashboard_tokens import build_dashboard_footer_html
            footer = await build_dashboard_footer_html(primary_employee_id)
        except Exception as e:
            logger.debug("Could not build dashboard footer: %s", e)
    await send_email(to=to, subject=subject, html_body=html_body, dashboard_footer=footer, **kwargs)


async def send_email(
    to: list[str],
    subject: str,
    html_body: str,
    cc: list[str] | None = None,
    importance: str = "Normal",
    attachments: list[dict] | None = None,
    dashboard_footer: str = "",
):
    full_body = html_body + dashboard_footer if dashboard_footer else html_body
    valid_to = [addr for addr in to if addr]
    if not valid_to:
        logger.warning("No valid recipients for email: %s", subject)
        return
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": full_body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in valid_to],
        "importance": importance,
    }

    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": addr}} for addr in cc if addr]

    if attachments:
        message["attachments"] = attachments

    path = f"/users/{settings.SENDER_EMAIL}/sendMail"
    await graph_client.post(path, json={"message": message, "saveToSentItems": True})
    logger.info("Email sent to %s — subject: %s", to, subject)
