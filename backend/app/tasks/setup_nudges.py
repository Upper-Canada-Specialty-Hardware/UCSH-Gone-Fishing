"""Tell the people who create Staff Directory records when one of them is broken.

The setup sweep can already name every record that would stall a request, but
knowing is not telling: the list sits on a dashboard tab that nobody opens until
somebody is already blocked, which is how a record with no supervisor had three
leave requests stall before anyone looked. This task closes that gap by mailing
the problem to the one person who can fix it.

Who gets told is the record's CREATOR, and nobody else. There is deliberately no
fallback: the only other identity on a list item is the last editor, which in
this tenant is an HR shared mailbox that would then receive a nudge for every
broken record in the directory. A record created by the app itself carries an
`application` identity with no user at all, so it is skipped and logged rather
than sent somewhere arbitrary.

Cadence is daily to sweep, weekly to re-nudge. A record broken in a NEW way is
mailed straight away rather than waiting out the week, because the signature of
what is wrong is part of what was said. Once a record stops being flagged its
row is deleted, so a later breakage opens a fresh conversation rather than
resuming an old one.

The sweep claims the day before it runs, not after, so a crash part-way through
cannot re-send everything on restart - a missed record is picked up by
tomorrow's run, which is the cheaper failure.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.models import StaffSetupNudge
from app.services.employee_validation import validate_all_employee_setups
from app.services.idempotency import claim_action
from app.services.setup_problem_copy import PROBLEM_INFO
from app.templates_render import render_staff_setup_issues

logger = logging.getLogger(__name__)

TORONTO_TZ = ZoneInfo("America/Toronto")
CHECK_INTERVAL = 60  # seconds between checks

# How long a record stays broken before its creator hears about it again.
NUDGE_REPEAT_DAYS = 7

# Local hour the daily sweep runs at. Morning, so a fix can land in the same
# working day rather than sitting overnight.
SWEEP_HOUR_LOCAL = 7

# Action name for the once-a-day claim. Namespaced under the Staff Directory
# list with the Toronto date as the item id, so the claim is one per day.
SWEEP_ACTION = "setup_nudge_sweep"


def decide(existing_row, signature: str, now: datetime) -> str:
    """Decide whether this record's creator hears from us, and why.

    Pure, so the cadence can be reasoned about without a database: the sweep
    only has to supply the row it already loaded.

    Args:
        existing_row: The StaffSetupNudge row for this record, or None when the
            record has never been nudged.
        signature: The sorted fail codes joined by commas, as they are now.
        now: The moment the sweep is running.

    Returns:
        "send_first", "send_changed", "send_weekly" or "skip".
    """
    if existing_row is None:
        return "send_first"
    if existing_row.issue_signature != signature:
        # Something else is wrong with the record now. That is a different
        # message, so it goes out rather than waiting for the weekly slot.
        return "send_changed"
    last_sent = existing_row.last_sent_at
    if last_sent.tzinfo is None:
        # SQLite hands back naive datetimes even from a timezone=True column.
        # Reading it as UTC matches what was written; treating it as local time
        # would shift the cadence by hours.
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    if now - last_sent >= timedelta(days=NUDGE_REPEAT_DAYS):
        return "send_weekly"
    return "skip"


def issues_for(row: dict) -> list[dict]:
    """Turn one flagged sweep row into the blocks the email lists.

    Only fails are carried, which is the same rule that flagged the record:
    warns are worth a look but do not stall a request, and a nudge that mixes
    the two teaches the reader to skim.

    Args:
        row: A flagged row from `validate_all_employee_setups`.

    Returns:
        One {"code", "title", "detail", "fix"} per failing check. An unknown
        code falls back to showing the code itself with no fix, so a check added
        to the validator without its wording still reaches the reader.
    """
    issues = []
    for fail in row.get("fails", []):
        code = fail.get("code", "")
        copy = PROBLEM_INFO.get(code, {})
        issues.append({
            "code": code,
            "title": copy.get("title", code),
            "detail": fail.get("detail", ""),
            "fix": copy.get("fix", ""),
        })
    return issues


def _signature(row: dict) -> str:
    """The problem set as a comparable string: sorted fail codes, comma joined."""
    return ",".join(sorted(fail.get("code", "") for fail in row.get("fails", [])))


async def _load_rows() -> dict[str, StaffSetupNudge]:
    """Every nudge row, by employee id. One read for the whole sweep."""
    async with async_session() as session:
        result = await session.execute(select(StaffSetupNudge))
        return {row.employee_id: row for row in result.scalars().all()}


async def _record_sent(employee_id: str, signature: str, recipient: str, now: datetime) -> None:
    """Write down that this record's creator has just been told.

    ``first_sent_at`` survives every re-nudge, so the row also says how long the
    record has been broken.

    Args:
        employee_id: Staff Directory item id of the broken record.
        signature: The problem set that was described in the email.
        recipient: The creator it was sent to.
        now: When it went out.
    """
    async with async_session() as session:
        row = await session.get(StaffSetupNudge, employee_id)
        if row is None:
            session.add(StaffSetupNudge(
                employee_id=employee_id,
                issue_signature=signature,
                recipient=recipient,
                first_sent_at=now,
                last_sent_at=now,
                send_count=1,
            ))
        else:
            row.issue_signature = signature
            row.recipient = recipient
            row.last_sent_at = now
            row.send_count = row.send_count + 1
        await session.commit()


async def _forget(employee_ids: list[str]) -> None:
    """Drop the rows of records that are no longer flagged."""
    async with async_session() as session:
        await session.execute(
            delete(StaffSetupNudge).where(StaffSetupNudge.employee_id.in_(employee_ids))
        )
        await session.commit()


async def _send_nudge(row: dict, issues: list[dict]) -> None:
    """Email one record's creator. Theirs is the only address used.

    Args:
        row: The flagged sweep row, carrying the creator and the record link.
        issues: The blocks `issues_for` built for it.
    """
    subject = "Staff Directory record needs attention - " + row["employee_name"]
    await send_email(
        to=[row["creator_email"]],
        subject=subject,
        html_body=render_staff_setup_issues(
            employee_name=row["employee_name"],
            issues=issues,
            record_url=row.get("record_url"),
        ),
    )


async def run_setup_nudge_sweep(now: datetime | None = None) -> dict:
    """Grade every Staff Directory record and nudge the creators of broken ones.

    Args:
        now: The moment the weekly cadence is measured from. Defaults to now.

    Returns:
        {"checked", "flagged", "sent", "skipped_unresolvable", "skipped_recent",
        "resolved"}.
    """
    now = now or datetime.now(timezone.utc)
    result = await validate_all_employee_setups()
    flagged = result["flagged"]
    existing = await _load_rows()

    sent = 0
    skipped_unresolvable = 0
    skipped_recent = 0

    for row in flagged:
        employee_id = row["employee_id"]
        creator_email = row.get("creator_email")
        if not creator_email:
            # Created by the app, or by somebody the item records no address
            # for. There is no second-best recipient, so this is logged and left
            # to the Employee Setup tab.
            skipped_unresolvable += 1
            logger.warning(
                "Setup nudge: no creator to notify for record #%s (%s)",
                employee_id, row.get("employee_name", ""),
            )
            continue

        signature = _signature(row)
        if decide(existing.get(employee_id), signature, now) == "skip":
            skipped_recent += 1
            continue

        try:
            await _send_nudge(row, issues_for(row))
        except Exception:  # noqa: BLE001 - one bad address must not end the sweep
            logger.exception(
                "Setup nudge failed for record #%s (%s)", employee_id, creator_email,
            )
            # Deliberately no row written: nothing was said, so tomorrow's run
            # treats this as a first nudge rather than as already handled.
            continue
        await _record_sent(employee_id, signature, creator_email, now)
        sent += 1

    # A record that is no longer flagged is forgotten entirely, so if it breaks
    # again months later its creator gets a first email rather than a re-nudge
    # carrying a count from the last time.
    flagged_ids = {row["employee_id"] for row in flagged}
    stale = [employee_id for employee_id in existing if employee_id not in flagged_ids]
    if stale:
        await _forget(stale)

    summary = {
        "checked": result["total_checked"],
        "flagged": len(flagged),
        "sent": sent,
        "skipped_unresolvable": skipped_unresolvable,
        "skipped_recent": skipped_recent,
        "resolved": len(stale),
    }
    logger.info(
        "Setup nudge sweep: checked %s, flagged %s, sent %s, no creator %s, "
        "still recent %s, resolved %s",
        summary["checked"], summary["flagged"], summary["sent"],
        summary["skipped_unresolvable"], summary["skipped_recent"], summary["resolved"],
    )
    return summary


async def _run_for_today(now: datetime) -> bool:
    """Run the sweep once for the Toronto day `now` falls in.

    The claim is inserted BEFORE the sweep runs, so a crash part-way through
    cannot make a restart re-send everything that had already gone out.

    Args:
        now: Local Toronto time.

    Returns:
        True when this call ran the sweep.
    """
    if now.hour < SWEEP_HOUR_LOCAL:
        return False
    if not await claim_action(
        settings.SP_LIST_STAFF_DIRECTORY, now.date().isoformat(), SWEEP_ACTION,
    ):
        return False
    await run_setup_nudge_sweep()
    return True


async def _loop() -> None:
    """Background task: check every 60s whether today's sweep is due."""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            if not settings.PROCESSING_ENABLED:
                continue
            await _run_for_today(datetime.now(TORONTO_TZ))
        except Exception:
            logger.exception("Setup nudge loop error")


def start_setup_nudge_task() -> asyncio.Task:
    return asyncio.create_task(_loop())
