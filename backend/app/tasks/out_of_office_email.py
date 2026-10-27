"""The weekday 7:00 am "on leave starting today" email (#466).

Every minute the loop looks at the Toronto clock. On a weekday between 7:00 and
10:00 it tries to send today's email once: the send is claimed first in
``processing_log`` (one row per day, unique), so with more than one server
running only the first claim sends. A server started after 10:00 sends nothing
that day, so a mid-day deploy never mails a stale morning list.
"""
import asyncio
import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.config import settings
from app.graph.email import send_email_via_mailer
from app.services.idempotency import claim_action
from app.services.out_of_office import day_label, is_holiday_everywhere, people_starting_leave, subject_for

logger = logging.getLogger(__name__)

TORONTO_TZ = ZoneInfo("America/Toronto")
CHECK_INTERVAL = 60                     # seconds between clock checks
SEND_FROM = time(7, 0)                  # 7:00 am Toronto, all year (zoneinfo follows daylight saving)
SEND_UNTIL = time(10, 0)                # no send after this, e.g. after a late restart
CLAIM_LIST = "out_of_office_daily"      # processing_log list_id; item_id is the day
CLAIM_ACTION = "sent"


def due_now(now: datetime) -> bool:
    """Whether a Toronto time falls in the weekday send window.

    Args:
        now: The current time in Toronto.

    Returns:
        True Monday to Friday from 7:00 until 10:00.
    """
    return now.weekday() < 5 and SEND_FROM <= now.time() < SEND_UNTIL


async def send_daily_email(today: date) -> bool:
    """Send today's email once, if it should go out at all.

    Skipped when no group address is set, when processing is off, when today is
    a company holiday in every listed province, or when nobody's leave starts
    today (an empty notice is noise; the skip is logged, so a quiet day can be
    told apart from a failed one). Then the day is claimed, so a second server
    (or a second minute) never sends it again; a send that fails
    after the claim is logged, not retried, so the group never gets it twice.
    It goes through the UCSH mailer (Microsoft HVE) only: the group takes mail
    from company senders, so there is no SMTP2GO fallback.

    Args:
        today: Today's date in Toronto.

    Returns:
        True when this call sent the email.
    """
    recipient = settings.OUT_OF_OFFICE_EMAIL_TO.strip()
    if not recipient or not settings.PROCESSING_ENABLED:      # off until configured, and in reporting-only mode
        return False
    if await is_holiday_everywhere(today):                    # nobody is working: nothing to say
        return False
    people = await people_starting_leave(today)
    if not people:                                            # nobody starts leave: no email today
        logger.info("Out-of-office email skipped for %s: nobody starts leave", today.isoformat())
        return False
    if not await claim_action(CLAIM_LIST, today.isoformat(), CLAIM_ACTION):
        return False                                          # already sent today, here or on another server

    from app.templates_render import render_out_of_office_daily
    html = render_out_of_office_daily(day_label=day_label(today), people=people)
    await send_email_via_mailer(                              # HVE only; raises rather than falling back
        to=[recipient], subject=subject_for(today), html_body=html,
        idempotency_key=f"{CLAIM_LIST}-{today.isoformat()}",  # the mailer drops a repeat of the same day
    )
    logger.info("Out-of-office email sent for %s: %d starting leave", today.isoformat(), len(people))
    return True


async def _out_of_office_loop():
    """Background task: once a minute, send today's email when it is due."""
    handled: date | None = None                               # the day this server already dealt with
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            now = datetime.now(TORONTO_TZ)
            if due_now(now) and now.date() != handled:
                await send_daily_email(now.date())
                handled = now.date()                          # sent or skipped: no more claims today
        except Exception:
            logger.exception("Out-of-office email loop error")


def start_out_of_office_email_task() -> asyncio.Task:
    """Start the daily out-of-office email loop."""
    return asyncio.create_task(_out_of_office_loop())
