"""Keep everyone's dashboard link alive without waiting for request activity.

A dashboard link lasts 30 days and is only ever minted while sending someone an
email. Whether a person keeps access therefore depends on whether email happens
to reach them, which decays very differently by role: a manager is topped up by
every approval request and every reminder across their whole team, while an
employee is topped up only by their own submissions and outcomes. On a team
where nobody submits anything for a month, no email is sent to anyone on it and
both the employee and their manager silently lose access.

This task is the floor underneath that. It renews anyone whose link is close to
expiring, so the replacement arrives before the old one dies. Nothing about the
existing emails changes — they still mint links exactly as before, and this only
fires for people the normal flow has stopped reaching.

Two properties worth keeping if this is ever changed:

  * Sends are claimed before they go out, through the same unique-insert used
    for approvals, so two Railway replicas cannot both renew the same person and
    a restart mid-run cannot re-send.
  * People with no row are seeded rather than emailed. Their expiries are spread
    across a month so the first run after deployment does not put the whole
    company through a ten-per-minute rate limit at once.
"""

import asyncio
import logging
from datetime import timedelta

from app.config import settings
from app.models.mixins import utcnow
from app.services.dashboard_link_tracking import find_expiring, seed_missing
from app.services.dashboard_tokens import DEFAULT_EXPIRY_DAYS
from app.services.idempotency import claim_action

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 86400  # seconds between scans (daily)

# How far ahead of expiry a link is replaced. Must comfortably exceed the scan
# interval: renewal can only happen on a run, so the lead time is what absorbs
# runs missed to a deploy or an outage. Seven days survives six consecutive
# failures and puts renewal on day 23 of 30.
RENEWAL_LEAD_DAYS = 7

# Window the seeder distributes first-time expiries over. Matching the link
# lifetime means roughly a thirtieth of people come due on any given day.
SEED_SPREAD_DAYS = 30

# Namespace for the idempotency claim. Not a SharePoint list id — claim_action's
# first argument is just a key space, and this keeps renewal claims from
# colliding with request processing.
CLAIM_NAMESPACE = "dashboard-link-renewal"


async def renew_expiring_links() -> int:
    """Email a fresh dashboard link to anyone whose current one is nearly dead.

    Seeds anybody not yet tracked first, so people whose link lapsed before this
    table existed come into scope rather than being invisible forever.

    Returns:
        How many renewal emails were sent.
    """
    from app.repositories.factory import get_employee_repository

    # Seeding needs the full directory; renewal does not. One list read per run
    # regardless of how many people are due, which is the part that has to stay
    # true — resolving roles per person would make this scale with headcount.
    # Read through the repository seam so this keeps working unchanged when the
    # employee domain moves to Postgres.
    try:
        employees = await get_employee_repository().get_all()
        await seed_missing([e.get("id") for e in employees if e.get("id")], SEED_SPREAD_DAYS)
    except Exception:  # noqa: BLE001 - seeding failures retry next run
        logger.exception("Dashboard link renewal: could not seed from the Staff Directory")

    cutoff = utcnow() + timedelta(days=RENEWAL_LEAD_DAYS)
    due = await find_expiring(cutoff)
    if not due:
        return 0

    logger.info("Dashboard link renewal: %d link(s) expiring before %s", len(due), cutoff.date())

    sent = 0
    for employee_id in due:
        try:
            if await _renew_one(employee_id):
                sent += 1
        except Exception:  # noqa: BLE001 - one failure must not stop the rest
            logger.exception("Dashboard link renewal failed for employee #%s", employee_id)
    return sent


async def _renew_one(employee_id: str) -> bool:
    """Send one person their replacement link.

    Args:
        employee_id: Staff Directory item id to renew.

    Returns:
        True when an email went out.
    """
    from app.graph.email import send_email_with_dashboard
    from app.services.employee import get_employee_by_id
    from app.templates_render import render_dashboard_link_renewal

    emp = await get_employee_by_id(employee_id)
    if not emp:
        # Left the directory. Their row stays put and simply keeps matching; it
        # is not deleted here, because a failed lookup is not proof of removal.
        logger.info("Dashboard link renewal: no Staff Directory record for #%s", employee_id)
        return False

    fields = emp.get("fields", {})
    email = (fields.get("EmailAddress") or "").strip()
    if not email:
        logger.info("Dashboard link renewal: employee #%s has no email address", employee_id)
        return False

    # Claimed per person per day. Two replicas scanning at once, or a restart
    # part-way through a run, produce one email rather than two.
    today = utcnow().date().isoformat()
    if not await claim_action(CLAIM_NAMESPACE, employee_id, today):
        return False

    html = render_dashboard_link_renewal(
        employee_name=fields.get("Title", ""), expiry_days=DEFAULT_EXPIRY_DAYS,
    )
    # Sent through the footer path on purpose: it appends whichever dashboards
    # this person has and records the new expiry, so renewal needs no link
    # handling of its own and cannot drift from how every other email does it.
    await send_email_with_dashboard(
        to=[email],
        subject="Your Dashboard Link",
        html_body=html,
        primary_employee_id=employee_id,
    )
    logger.info("Dashboard link renewed for employee #%s", employee_id)
    return True


async def _renewal_loop():
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if not settings.PROCESSING_ENABLED:
                continue
            sent = await renew_expiring_links()
            if sent:
                logger.info("Dashboard link renewal: %d link(s) renewed", sent)
        except Exception:
            logger.exception("Dashboard link renewal loop error")


def start_dashboard_link_task() -> asyncio.Task:
    return asyncio.create_task(_renewal_loop())
