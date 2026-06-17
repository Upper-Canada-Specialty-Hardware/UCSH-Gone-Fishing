"""Periodic reminder emails for approval requests left pending too long.

A request gets its first reminder once it has been pending for FIRST_REMINDER_DAYS,
then another every REPEAT_REMINDER_DAYS, until it is actioned or its effective date
has passed. Each reminder re-sends the manager approval email with fresh links and
bumps the approval version, so the previously emailed links are superseded (clicking
an older link then shows the "use the newest email" page).
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models import RequestApprovalState

logger = logging.getLogger(__name__)

CHECK_INTERVAL = 3600  # seconds between scans (hourly)
FIRST_REMINDER_DAYS = 30
REPEAT_REMINDER_DAYS = 7
# CO/PO requests have no event date to key off; stop after this many reminders.
MAX_REMINDERS_WITHOUT_DATE = 6


def _request_type(list_id: str) -> str | None:
    return {
        settings.SP_LIST_LEAVE_REQUESTS: "leave",
        settings.SP_LIST_OVERTIME_REQUESTS: "overtime",
        settings.SP_LIST_CARRYOVER_PAYOUT: "carryover-payout",
    }.get(list_id)


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _is_processed(fields: dict, req_type: str) -> bool:
    """True when the request is no longer awaiting a manager decision.

    Mirrors the per-type predicates used by the SMS handler so reminders stop as
    soon as the request has been approved/rejected through any channel.
    """
    if fields.get("Status") != "Pending":
        return True
    if req_type == "leave":
        return fields.get("ApproveProcessedFlag") == "Processed"
    if req_type == "carryover-payout":
        return fields.get("SystemState") == "Processed"
    return False  # overtime: the Status check above is sufficient


def _cutoff_passed(req_type: str, fields: dict, item: dict, reminder_count: int) -> bool:
    """True when the request is moot and reminders should stop."""
    today = date.today()
    if req_type in ("leave", "overtime"):
        start = _parse_date(fields.get("StartDate"))
        return start is not None and start < today
    if req_type == "carryover-payout":
        # No event date: moot after year-end of the request's year, capped by count.
        created = _parse_date(item.get("createdDateTime"))
        if created is not None and today.year > created.year:
            return True
        return reminder_count >= MAX_REMINDERS_WITHOUT_DATE
    return False


def _is_due(row: RequestApprovalState, now: datetime) -> bool:
    threshold = FIRST_REMINDER_DAYS if row.reminder_count == 0 else REPEAT_REMINDER_DAYS
    return now - row.last_emailed_at >= timedelta(days=threshold)


async def _close(list_id: str, item_id: str) -> None:
    async with async_session() as session:
        row = await session.get(RequestApprovalState, (list_id, item_id))
        if row and not row.reminders_closed:
            row.reminders_closed = True
            await session.commit()


async def _resend(req_type: str, item_id: str, fields: dict) -> None:
    if req_type == "leave":
        from app.services.leave_requests import send_approval_email
        await send_approval_email(item_id, is_reminder=True)
    elif req_type == "overtime":
        from app.services.overtime_requests import send_approval_email
        from app.services.employee import resolve_person_field, get_all_managers_for_employee
        employee = await resolve_person_field(
            fields.get("SubmittedBy") or fields.get("SubmittedByLookupId")
        )
        if not employee:
            return
        managers = await get_all_managers_for_employee(employee)
        if not managers:
            return
        await send_approval_email(item_id, employee, managers, is_reminder=True)
    elif req_type == "carryover-payout":
        from app.services.carryover_payout import send_approval_email
        await send_approval_email(item_id, is_reminder=True)


async def _process_row(row: RequestApprovalState) -> None:
    req_type = _request_type(row.list_id)
    if req_type is None:
        await _close(row.list_id, row.item_id)
        return

    item = await sp_client.get_list_item(row.list_id, row.item_id)
    fields = item.get("fields", {})

    if _is_processed(fields, req_type):
        await _close(row.list_id, row.item_id)
        logger.info("Reminders closed for %s #%s - already actioned", req_type, row.item_id)
        return

    if _cutoff_passed(req_type, fields, item, row.reminder_count):
        await _close(row.list_id, row.item_id)
        logger.info("Reminders closed for %s #%s - past cutoff", req_type, row.item_id)
        return

    logger.info(
        "Reminder due for %s #%s - re-sending (prior reminders: %d)",
        req_type, row.item_id, row.reminder_count,
    )
    await _resend(req_type, row.item_id, fields)


async def send_due_reminders() -> None:
    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(RequestApprovalState).where(RequestApprovalState.reminders_closed.is_(False))
        )
        rows = result.scalars().all()

    due = [r for r in rows if _is_due(r, now)]
    if not due:
        return
    logger.info("Reminder scan - %d request(s) due", len(due))
    for row in due:
        try:
            await _process_row(row)
        except Exception:
            logger.exception("Reminder failed for %s #%s", row.list_id, row.item_id)


async def _reminder_loop() -> None:
    """Background task: scan for pending requests due a reminder."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if not settings.PROCESSING_ENABLED:
                continue
            await send_due_reminders()
        except Exception:
            logger.exception("Reminder loop error")


def start_reminder_task() -> asyncio.Task:
    return asyncio.create_task(_reminder_loop())
