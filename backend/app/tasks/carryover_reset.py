"""Annual CarryOver balance reset — zeroes all employees' CarryOver on April 1st midnight ET."""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.graph.sharepoint import sp_client
from app.models.carryover_reset_log import CarryoverResetLog
from app.services.balance import recalculate_request_allow_date
from app.services.concurrency import lock_manager
from app.services.employee import ADMIN_NAMES, get_employee_by_name

logger = logging.getLogger(__name__)

TORONTO_TZ = ZoneInfo("America/Toronto")
CHECK_INTERVAL = 60  # seconds between checks


async def _carryover_reset_loop():
    """Background task: check every 60s if it's April 1 and time to reset."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            now = datetime.now(TORONTO_TZ)
            if now.month == 4 and now.day == 1:
                if not await _already_ran_this_year(now.year):
                    if settings.PROCESSING_ENABLED:
                        logger.info("CarryOver reset triggered for year %d", now.year)
                        await _execute_carryover_reset(now.year)
                    else:
                        logger.info("CarryOver reset due but PROCESSING_ENABLED is False — skipping")
        except Exception:
            logger.exception("CarryOver reset loop error")


def start_carryover_reset_task() -> asyncio.Task:
    return asyncio.create_task(_carryover_reset_loop())


async def _already_ran_this_year(year: int) -> bool:
    async with async_session() as session:
        result = await session.execute(
            select(CarryoverResetLog).where(CarryoverResetLog.year == year)
        )
        return result.scalar_one_or_none() is not None


async def _execute_carryover_reset(year: int):
    """Fetch employees, zero carryover, send emails, log completion."""
    all_employees = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)

    affected = []
    for emp in all_employees:
        fields = emp.get("fields", {})
        carryover = float(fields.get("CarryOver", 0) or 0)
        if carryover != 0:
            affected.append({
                "id": emp["id"],
                "name": fields.get("Title", "Unknown"),
                "email": fields.get("EmailAddress", ""),
                "carryover": carryover,
                "vacation": float(fields.get("CurrentVacationBalance", 0) or 0),
            })

    if not affected:
        logger.info("No employees with non-zero CarryOver — nothing to reset")
        await _record_completion(year)
        return

    logger.info("Found %d employees with non-zero CarryOver to reset", len(affected))

    succeeded = []
    failed = []

    for emp_info in affected:
        try:
            await _reset_single_employee(emp_info)
            succeeded.append(emp_info)
        except Exception:
            logger.exception(
                "Failed to reset CarryOver for %s (ID %s)",
                emp_info["name"], emp_info["id"],
            )
            failed.append(emp_info)

    # Send individual notification emails
    for emp_info in succeeded:
        try:
            await _send_employee_notification(emp_info)
        except Exception:
            logger.exception(
                "Failed to send CarryOver reset email to %s", emp_info["name"]
            )

    # Send admin summary
    try:
        await _send_admin_summary(year, succeeded, failed)
    except Exception:
        logger.exception("Failed to send CarryOver reset admin summary email")

    await _record_completion(year)
    logger.info(
        "CarryOver reset complete: %d succeeded, %d failed",
        len(succeeded), len(failed),
    )


async def _reset_single_employee(emp_info: dict):
    """Zero out CarryOver for one employee, then recalculate RequestAllowDate."""
    emp_id = emp_info["id"]
    async with lock_manager.lock(emp_id):
        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY,
            emp_id,
            {"CarryOver": 0},
        )
        await recalculate_request_allow_date(
            emp_id,
            vacation=emp_info["vacation"],
            carryover=0,
        )


async def _send_employee_notification(emp_info: dict):
    if not emp_info["email"]:
        logger.warning("No email for %s — skipping notification", emp_info["name"])
        return

    from app.templates_render import render_carryover_reset_employee
    html = render_carryover_reset_employee(
        employee_name=emp_info["name"],
        carryover_lost=emp_info["carryover"],
    )
    await send_email(
        to=[emp_info["email"]],
        subject="Annual Carry Over Balance Reset",
        html_body=html,
    )


async def _send_admin_summary(year: int, succeeded: list[dict], failed: list[dict]):
    admin_emails = []
    for admin_name in ADMIN_NAMES:
        admin = await get_employee_by_name(admin_name)
        if admin:
            email = admin.get("fields", {}).get("EmailAddress", "")
            if email:
                admin_emails.append(email)

    if not admin_emails:
        logger.error("No admin emails found — cannot send CarryOver reset summary")
        return

    from app.templates_render import render_carryover_reset_admin_summary
    html = render_carryover_reset_admin_summary(
        year=year, succeeded=succeeded, failed=failed,
    )
    await send_email(
        to=admin_emails,
        subject=f"Carry Over Reset Summary — {year}",
        html_body=html,
        importance="High",
    )


async def _record_completion(year: int):
    async with async_session() as session:
        session.add(CarryoverResetLog(year=year))
        await session.commit()
    logger.info("Recorded CarryOver reset completion for year %d", year)
