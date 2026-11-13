"""The January 1 refresh and automatic year-end carry-over (#553, #319, #546).

On January 1 (Toronto), for each salaried person: unused vacation moves to their
carried-over days (``CarryOver``), usable until March 31, and vacation is
refilled to its yearly entitlement; sick is refilled to its entitlement; the
yearly payout total starts again at 0. Hourly staff get their sick refill only.
A negative leftover is time owed: the new entitlement pays it first (#546). The
April 1 reset then clears what is left of the carried-over days, as before.

It is the "actioned carry-over" management approved: done by the system, no form,
no manager. It only runs while ``YEAR_END_CARRY_ENABLED`` is on, so it can never
clash with HR refreshing balances by hand, and each person is handled once per
year, recorded in ``year_end_carry_log``.
"""

import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.models.year_end_carry_log import YearEndCarryLog
from app.repositories.staff_store import staff_store
from app.services.concurrency import lock_manager
from app.services.employee import ADMIN_NAMES, get_employee_by_name
from app.services.employment import is_hourly

logger = logging.getLogger(__name__)

TORONTO_TZ = ZoneInfo("America/Toronto")
CHECK_INTERVAL = 60          # seconds between checks, like the April 1 reset
RUN_AFTER_MINUTE = 5         # 00:05 on Jan 1, clear of midnight
HISTORY_REASON = "January 1 refresh and automatic year end carry-over."


def sick_refill_pure(sick: float, entitlement: float) -> float:
    """Sick after the January 1 refill. Pure.

    Args:
        sick: Sick left at the end of the year.
        entitlement: The yearly sick entitlement.

    Returns:
        The entitlement, less any days owed; positive leftovers do not carry.
    """
    return entitlement + (sick if sick < 0 else 0.0)


def carry_pure(vacation: float, carry_over: float, entitlement: float) -> dict:
    """The balances after the year-end carry. Pure.

    Args:
        vacation: The current-year vacation left at the end of the year.
        carry_over: The next-year line before the carry.
        entitlement: The yearly vacation entitlement.

    Returns:
        ``{"CurrentVacationBalance", "CarryOver", "moved"}``: the new values and how
        many days moved to the next-year line.
    """
    moved = vacation if vacation > 0 else 0.0                 # only a positive leftover carries
    debt = vacation if vacation < 0 else 0.0                  # a negative leftover is owed (#546)
    return {
        "CurrentVacationBalance": entitlement + debt,         # the new year's days, debt paid first
        "CarryOver": max(carry_over, 0.0) + moved,            # next-year line never below 0
        "moved": moved,
    }


async def _year_end_loop() -> None:
    """Check every minute whether it is Jan 1, past 00:05, and time to carry."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            now = datetime.now(TORONTO_TZ)
            if now.month == 1 and now.day == 1 and (now.hour > 0 or now.minute >= RUN_AFTER_MINUTE):
                if settings.YEAR_END_CARRY_ENABLED and settings.PROCESSING_ENABLED:
                    await run_year_end_carry(now.year - 1)    # carry the year that just ended
        except Exception:
            logger.exception("Year-end carry loop error")


def start_year_end_carry_task() -> asyncio.Task:
    """Start the background loop.

    Returns:
        The task, cancelled at shutdown.
    """
    return asyncio.create_task(_year_end_loop())


async def _done_ids(year: int) -> set[str]:
    """The people already carried for this year.

    Args:
        year: The year that ended.

    Returns:
        Their staff ids.
    """
    async with async_session() as session:
        rows = (await session.execute(
            select(YearEndCarryLog.employee_id).where(YearEndCarryLog.year == year)
        )).scalars().all()
    return set(rows)


async def _mark_done(year: int, employee_id: str) -> None:
    """Record one person as carried for this year.

    Args:
        year: The year that ended.
        employee_id: Their staff id.
    """
    async with async_session() as session:
        session.add(YearEndCarryLog(year=year, employee_id=employee_id))
        await session.commit()


def _archived(fields: dict) -> bool:
    """Whether a staff record is archived; never raises.

    Args:
        fields: The staff fields.

    Returns:
        True when archived.
    """
    try:
        from app.repositories.postgres.staff import is_archived
        return is_archived(fields)
    except Exception:  # noqa: BLE001 - SharePoint-shaped rows have no archive state
        return False


async def run_year_end_carry(year: int) -> dict:
    """Carry every salaried person's unused vacation from ``year`` into the next.

    Args:
        year: The year that just ended.

    Returns:
        ``{"carried": [...], "failed": [...]}``, one entry per person handled now.
    """
    done = await _done_ids(year)                              # people a crashed run already handled
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    carried: list[dict] = []
    failed: list[dict] = []
    for item in items:
        emp_id = str(item.get("id"))
        fields = item.get("fields", {})
        if emp_id in done or _archived(fields):
            continue                                          # once per year, active people only
        name = fields.get("Title", "Unknown")
        try:
            async with lock_manager.lock(emp_id):             # same lock as every balance write
                fresh = await staff_store.get_list_item(settings.SP_LIST_STAFF_DIRECTORY, emp_id)
                ff = fresh.get("fields", {})
                writes = {"CurrentSickDayBalance": sick_refill_pure(   # everyone's sick refills (#319)
                    float(ff.get("CurrentSickDayBalance", 0) or 0), float(ff.get("SickDayEntitlement", 0) or 0),
                )}
                result = {"moved": 0.0}
                if not is_hourly(ff):                         # hourly have no vacation balance or payout
                    result = carry_pure(
                        float(ff.get("CurrentVacationBalance", 0) or 0),
                        float(ff.get("CarryOver", 0) or 0),
                        float(ff.get("DefaultYearlyVacationDays", 0) or 0),
                    )
                    writes.update({
                        "CurrentVacationBalance": result["CurrentVacationBalance"],
                        "CarryOver": result["CarryOver"],
                        "Payout": 0.0,                        # the 5-day payout cap is per year
                    })
                await staff_store.update_list_item_fields(
                    settings.SP_LIST_STAFF_DIRECTORY, emp_id, writes,
                    history_source="system", history_reason=HISTORY_REASON, history_actor="system",
                )
                await _mark_done(year, emp_id)                # recorded right after the write
            carried.append({"name": name, "email": ff.get("EmailAddress", ""), "moved": result["moved"]})
        except Exception:
            logger.exception("Year-end carry failed for %s (ID %s)", name, emp_id)
            failed.append({"name": name})
    for person in carried:                                     # emails after every write, best effort
        if person["moved"] > 0 and person["email"]:
            try:
                await send_email(
                    to=[person["email"]],
                    subject=f"Your {year} vacation days moved to {year + 1}",
                    html_body=render_moved_email(person["name"], person["moved"], year),
                )
            except Exception:
                logger.exception("Year-end carry email failed for %s", person["name"])
    if carried or failed:
        try:
            await _send_admin_summary(year, carried, failed)
        except Exception:
            logger.exception("Year-end carry admin summary failed")
    logger.info("Year-end carry for %d: %d carried, %d failed", year, len(carried), len(failed))
    return {"carried": carried, "failed": failed}


def render_moved_email(name: str, moved: float, year: int) -> str:
    """The note to one person about the days moved.

    Args:
        name: Their name.
        moved: Days moved to the next-year line.
        year: The year that ended.

    Returns:
        The email HTML.
    """
    count = f"{moved:g} day{'' if moved == 1 else 's'}"
    return (
        f"<p>Hi {html.escape(name)},</p>"
        f"<p>{count} of your {year} vacation moved to {year + 1}. "
        f"Use them by March 31, {year + 1}.</p>"
    )


async def _send_admin_summary(year: int, carried: list[dict], failed: list[dict]) -> None:
    """Tell the admins who was carried and who failed.

    Args:
        year: The year that ended.
        carried: People carried this run.
        failed: People whose carry failed.
    """
    emails = []
    for admin_name in ADMIN_NAMES:
        admin = await get_employee_by_name(admin_name)
        email = (admin or {}).get("fields", {}).get("EmailAddress", "")
        if email:
            emails.append(email)
    if not emails:
        logger.error("No admin emails found for the year-end carry summary")
        return
    rows = "".join(
        f"<li>{html.escape(p['name'])}: {p['moved']:g} days moved</li>" for p in carried if p["moved"] > 0
    )
    failed_rows = "".join(f"<li>{html.escape(p['name'])}</li>" for p in failed)
    body = (
        f"<p>January 1 refresh for {year + 1}: {len(carried)} people refreshed, {len(failed)} failed.</p>"
        + (f"<p>Moved to {year + 1}:</p><ul>{rows}</ul>" if rows else "")
        + (f"<p>Failed, fix by hand:</p><ul>{failed_rows}</ul>" if failed_rows else "")
    )
    await send_email(to=emails, subject=f"Year-end carry summary for {year}", html_body=body, importance="High")
