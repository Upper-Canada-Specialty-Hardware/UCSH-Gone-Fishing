"""Hourly: remind supervisors, then admins, about requests on hold.

A request from someone not in the Staff Directory yet waits until their
supervisor adds them (services/held_requests.py). This loop sends the one
supervisor reminder after 2 business days and the one admin email after 5.
Each is recorded on the row, so restarts and repeated runs never resend.
"""

import asyncio
import logging

from app.config import settings
from app.services.held_requests import remind_held_requests

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 3600  # seconds between sweeps (hourly)


async def _loop() -> None:
    """Background task: sweep held requests every hour while processing is on."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if not settings.PROCESSING_ENABLED:
                continue                                       # read-only mode sends nothing
            sent = await remind_held_requests()
            if sent["supervisor"] or sent["admins"]:
                logger.info("Held request reminders sent: %s", sent)
        except Exception:
            logger.exception("Held request reminder loop error")


def start_held_request_reminder_task() -> asyncio.Task:
    """Start the hourly sweep; cancelled on shutdown."""
    return asyncio.create_task(_loop())
