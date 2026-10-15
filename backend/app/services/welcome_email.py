"""The welcome email a person gets once Add Employee creates their record (#227).

Sent from both dashboards' Add Employee (routes/dashboard.py
_after_employee_created). It goes through send_email_with_dashboard, so it also
ends with the standard footer linking to the site, and through send_email, so a
UCSH address goes out over the UCSH mailer and any other address over SMTP2GO,
the same as every other email.
"""

import logging

from app.config import settings
from app.graph.email import send_email_with_dashboard
from app.templates_render import render_welcome_employee

logger = logging.getLogger(__name__)

STAFF_TUTORIAL_PATH = "stafftutorial.html"     # served by the frontend next to the app (#230)
WELCOME_SUBJECT = "Welcome to UCSH Out of Office"


def site_links() -> tuple[str, str]:
    """The public site and the staff tutorial, from DASHBOARD_FRONTEND_URL.

    Returns:
        ``(site_url, tutorial_url)``; both blank when the frontend address is
        not configured (local development).
    """
    base = settings.DASHBOARD_FRONTEND_URL.rstrip("/")         # the one public address
    if not base:
        return "", ""
    return f"{base}/", f"{base}/{STAFF_TUTORIAL_PATH}"


async def send_welcome_email(record: dict) -> bool:
    """Email a newly added person how to sign in, with the tutorial link.

    Args:
        record: The Staff Directory record Add Employee just created
            (``{"id", "fields"}``).

    Returns:
        True when the email was handed to the email service; False when there
        was nothing to send to (no address, or no site address configured).

    Raises:
        Exception: Whatever the email service raised; the caller reports it
            and never undoes the add.
    """
    fields = record.get("fields", {})
    email = (fields.get("EmailAddress") or "").strip()
    site_url, tutorial_url = site_links()
    if not email or not site_url:
        return False                                           # nowhere to send, or no links to give
    first_name = (fields.get("Title") or "").strip().split(" ")[0] or "there"
    html = render_welcome_employee(first_name, email, site_url, tutorial_url)
    await send_email_with_dashboard(
        to=[email], subject=WELCOME_SUBJECT, html_body=html,
        primary_employee_id=record.get("id"),                  # appends the site-link footer
    )
    logger.info("Welcome email sent to new employee #%s", record.get("id"))
    return True
