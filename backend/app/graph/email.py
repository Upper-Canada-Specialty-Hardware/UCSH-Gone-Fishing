import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SMTP2GO_URL = "https://api.smtp2go.com/v3/email/send"
_http = httpx.AsyncClient(timeout=30.0)


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

    payload = {
        "api_key": settings.SMTP2GO_API_KEY,
        "sender": settings.SENDER_EMAIL,
        "to": valid_to,
        "subject": subject,
        "html_body": full_body,
    }

    if cc:
        valid_cc = [addr for addr in cc if addr]
        if valid_cc:
            payload["cc"] = valid_cc

    if importance and importance != "Normal":
        payload["custom_headers"] = [
            {"header": "X-Priority", "value": "1"},
            {"header": "Importance", "value": importance},
        ]

    resp = await _http.post(SMTP2GO_URL, json=payload)
    if resp.status_code >= 400:
        logger.error("SMTP2GO %d: %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()

    data = resp.json().get("data", {})
    if data.get("failed", 0) > 0:
        logger.error("SMTP2GO partial failure: %s", data.get("failures"))

    logger.info("Email sent to %s — subject: %s", valid_to, subject)
