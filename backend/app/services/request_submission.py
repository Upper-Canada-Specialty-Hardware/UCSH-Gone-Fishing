"""Finish a new request: record it, and queue its notifications (#323, #324, #326).

When a request is created the old way, the manager approval email and text and
the employee's receipt are sent straight away, so submitting waits on the email
provider and the request's timeline carries no ``submitted`` /
``sent_to_manager`` / ``receipt_sent`` events. This module closes that gap for
the Postgres path: it writes the request's identity in one transaction, appends
the ``submitted`` event, and only QUEUES the three messages to the outbox, so the
outbox worker delivers them and nothing is half done.

It runs only once requests AND staff both live in Postgres (``submission_active``,
the same gate the decision path uses). Until then every entry point keeps the old
SharePoint flow, which sends directly, byte-for-byte unchanged.

Every entry point funnels here: the request page and the ``/api/forms`` endpoints
(through ``auto_assign_manager``), a held request released once its submitter is
on staff (the same path), a Microsoft Form item found in SharePoint after the
switch (``services/sharepoint_check``), and a manager logging a sick day (which
is approved on the spot, so it takes only the ``submitted`` event, ``notify``
False). Each write carries a ``step_key`` that is unique in its table, so a
second call with the same keys, or a rerun of the SharePoint check, adds nothing.
"""
import logging
from datetime import date

from sqlalchemy import select

from app.database import async_session
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.approval_links import generate_approval_url
from app.services.balance import (
    is_next_year_request,
    simulate_leave_impact,
    simulate_overtime_impact,
)
from app.services.employee import (
    get_all_managers_for_employee,
    get_employee_by_email,
    get_employee_by_id,
)
from app.services.employment import (
    employment_type as staff_employment_type,
    floor_warning,
    floor_warning_text,
    is_hourly,
    leave_costs_balance,
)
from app.services.request_decisions import _wake_sender, decisions_active
from app.services.request_journal import append_event, enqueue_message, log_step, step_key
from app.services.request_submitter import resolve_request_submitter
from app import templates_render as tr

logger = logging.getLogger(__name__)

# The person column that names each kind's submitter, for the fallback resolve.
PERSON_COLUMN = {"leave": "SubmittedTest", "overtime": "SubmittedBy", "carryover_payout": "SubmittedBy"}
# RequestSource (what the intake stamps) -> the submitted_channel stored here.
SOURCE_TO_CHANNEL = {
    "Request page": "request_page",
    "API": "form",
    "Admin dashboard": "admin_dashboard",
    "Manager dashboard": "manager_dashboard",
}
# A carry-over/payout option stored on the row -> the word the emails and SMS use.
OPTION_WORD = {"carry_over": "Carry Over", "payout": "Payout"}


async def submission_active() -> bool:
    """Whether the Postgres submission path is the one that runs now.

    The same gate as the decision path: it runs only once requests AND staff both
    live in Postgres, because queuing the manager email needs the request row in
    Postgres and the content needs the staff record.

    Returns:
        True when a new request should record events and queue its messages here.
    """
    return await decisions_active()


def _channel_from_source(record: RequestRecord) -> str:
    """The submitted_channel for a request, from the RequestSource it was stamped with.

    Args:
        record: The request row (its ``extra`` carries RequestSource).

    Returns:
        A request_vocab channel; ``"form"`` for a Microsoft Form item (blank
        RequestSource) and anything unrecognised.
    """
    source = (record.extra or {}).get("RequestSource") or ""        # blank for the Microsoft Form
    return SOURCE_TO_CHANNEL.get(source, "form")


async def _load(session, kind: str, item_id) -> RequestRecord | None:
    """The request row for one (kind, item_id) in the given session, or None.

    Args:
        session: An open session.
        kind: The request kind.
        item_id: The item_id people and links use.

    Returns:
        The RequestRecord, or None when the id is not a number or not found.
    """
    try:
        key = int(item_id)
    except (TypeError, ValueError):
        return None
    return (await session.execute(
        select(RequestRecord).where(RequestRecord.kind == kind, RequestRecord.item_id == key)
    )).scalars().first()


async def _resolve_employee(record: RequestRecord, fields: dict) -> dict | None:
    """The submitter's Staff Directory record, by id, then email, then person column.

    Args:
        record: The request row.
        fields: The request in the SharePoint list-item shape (for the fallback).

    Returns:
        The employee item (``{"id", "fields"}``), or None when nothing matches.
    """
    if record.employee_id:
        employee = await get_employee_by_id(record.employee_id)     # the staff id already on the row
        if employee:
            return employee
    if record.employee_email:
        employee = await get_employee_by_email(record.employee_email)
        if employee:
            return employee
    return await resolve_request_submitter(fields, PERSON_COLUMN[record.kind])  # older person-column link


def _set_identity(record: RequestRecord, employee: dict | None, manager: dict | None, channel: str) -> None:
    """Stamp who the request is for and who decides it, from the staff records.

    Args:
        record: The request row to fill in place.
        employee: The submitter's staff record, or None.
        manager: The deciding manager's staff record, or None.
        channel: The submitted_channel to record.
    """
    record.submitted_channel = channel                              # where the request came in from
    if employee is not None:
        sf = employee.get("fields", {})
        record.employee_id = str(employee["id"])                    # the staff id (overrides any lookup id)
        record.employee_name = record.employee_name or sf.get("Title") or None
        record.employee_email = (
            record.employee_email or (sf.get("EmailAddress") or "").strip().lower() or None
        )
        record.employment_type = staff_employment_type(sf) or None  # the type that applied now; "" -> None for the CHECK
        record.location = record.location or sf.get("Location") or None
        record.department = record.department or sf.get("Department") or None
    if manager is not None:
        record.manager_id = str(manager["id"])                      # the deciding manager
        record.manager_name = manager.get("fields", {}).get("Title") or record.manager_name


async def _enqueue_notifications(session, record, employee, managers, *, event, suffix=None) -> None:
    """Queue the manager approval email and text and the employee receipt.

    One manager email (and a text when they have a cell) per manager, then one
    receipt to the employee. With a ``suffix`` (a date) and the reminder step the
    same rows become a once-a-day reminder instead, email only. Each row carries a
    step_key that is unique in the outbox, so a repeat queues nothing again.

    Args:
        session: The open session; not committed here.
        record: The request row.
        employee: The submitter's staff record.
        managers: The managers to ask (first is primary).
        event: The event the messages follow, for the outbox link, or None.
        suffix: A date string for a reminder (its key is per-day); None for a
            first submission, which also sends the receipt.
    """
    emp_fields = employee.get("fields", {})
    fields = TypedRequestList(record.kind)._item_shape(record)["fields"]   # SharePoint shape the templates read
    version = record.approval_version
    is_reminder = suffix is not None
    message_type = "reminder" if is_reminder else "manager_approval"
    # The step the key is built on: a dated reminder step, or the sent-to-manager step.
    manager_step = (
        step_key(record.kind, record.item_id, "reminder_sent", version, suffix=suffix)
        if is_reminder else
        step_key(record.kind, record.item_id, "sent_to_manager", version)
    )
    built = _build_manager_content(record, emp_fields, fields)      # per-kind subject + bodies

    for manager in managers:
        mgr_fields = manager.get("fields", {})
        manager_id = manager["id"]
        mgr_email = (mgr_fields.get("EmailAddress") or "").strip()
        subject, html, sms_body = built(manager_id)                # links are per manager
        if mgr_email:
            await enqueue_message(
                session, record, event, message_type, "email", mgr_email, subject, html,
                f"{manager_step}:email:{mgr_email}",
            )
        cell = (mgr_fields.get("CellNumber") or "").strip()
        if cell and not is_reminder:                               # reminders are email-only, with fresh links
            await enqueue_message(
                session, record, event, message_type, "sms", cell, None, sms_body,
                f"{manager_step}:sms:{cell}",
            )

    if is_reminder:
        return                                                      # a reminder re-asks the manager only
    email = record.employee_email
    if email:                                                      # the employee's "received" receipt
        receipt_subject, receipt_html = _build_receipt_content(record, emp_fields, fields)
        receipt_step = step_key(record.kind, record.item_id, "receipt_sent", version)
        await enqueue_message(
            session, record, event, "receipt", "email", email, receipt_subject, receipt_html,
            f"{receipt_step}:email:{email}",
        )


def _build_manager_content(record: RequestRecord, emp_fields: dict, fields: dict):
    """Return a function giving one manager's approval subject, email HTML and SMS.

    The content matches today's approval email, text and links, kind by kind. The
    links carry the request's current approval version, which is what the Postgres
    decision path checks, so an emailed link stays valid until an edit bumps it.

    Args:
        record: The request row.
        emp_fields: The submitter's staff fields (balances for the projection).
        fields: The request in the SharePoint list-item shape.

    Returns:
        ``make(manager_id) -> (subject, html, sms_body)``.
    """
    kind = record.kind
    item_id = record.item_id
    version = record.approval_version
    submitter_name = emp_fields.get("Title", "")

    if kind == "leave":
        leave_type = record.leave_type or ""
        days = float(record.days or 0)
        start, end = record.start_date, record.end_date or record.start_date
        is_next = bool(start and end and is_next_year_request(start, end))
        projected = (
            simulate_leave_impact(emp_fields, leave_type, days, is_next)
            if leave_costs_balance(emp_fields, leave_type) else None
        )
        below_zero = floor_warning(emp_fields, projected)           # red when approving goes below zero
        sms_dates = _leave_sms_dates(record.start_date, record.end_date)

        def make(manager_id):
            approve_url = generate_approval_url("leave", item_id, "approve", manager_id, approval_version=version)
            reject_url = generate_approval_url("leave", item_id, "reject", manager_id, approval_version=version)
            html = tr.render_leave_approval_email(
                fields, emp_fields, approve_url, reject_url, submitter_name, projected,
                conflict_warning=None, floor_warning=below_zero,
            )
            sms_body = (
                f"Leave Request #{item_id} for {submitter_name} ({days} days {leave_type}).\n"
                f"{sms_dates}"
                f"{_leave_sms_balance(projected, emp_fields, leave_type, below_zero)}"
                f'Reply "LR Approve {item_id}" or "LR Reject {item_id}"'
            )
            return f"Leave Request - {submitter_name}", html, sms_body
        return make

    if kind == "overtime":
        hours = float(record.hours or 0)
        projected = simulate_overtime_impact(emp_fields, hours)
        ot_date = record.start_date.strftime("%b %d, %Y") if record.start_date else ""
        bal_line = f"If approved: MU: {projected['CurrentOvertimeBalance']}.\n" if projected else ""

        def make(manager_id):
            approve_url = generate_approval_url("overtime", item_id, "approve", manager_id, approval_version=version)
            reject_url = generate_approval_url("overtime", item_id, "reject", manager_id, approval_version=version)
            html = tr.render_overtime_approval_email(
                fields, submitter_name, approve_url, reject_url, False,
                emp_fields=emp_fields, projected=projected,
            )
            sms_body = (
                f"Time Make-Up Request #{item_id} for {submitter_name} ({hours} hrs).\n"
                f"{ot_date}\n{bal_line}"
                f'Reply "OT Approve {item_id}" or "OT Reject {item_id}"'
            )
            return f"Overtime Request - {submitter_name}", html, sms_body
        return make

    # carry-over / payout
    days = float(record.days or 0)
    request_type = OPTION_WORD.get(record.request_option or "", "Carry Over")
    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)
    new_vacation = current_vacation - days
    if request_type == "Carry Over":
        new_carryover, new_payout = current_carryover + days, current_payout
    else:
        new_carryover, new_payout = current_carryover, current_payout + days

    def make(manager_id):
        approve_url = generate_approval_url("carryover-payout", item_id, "approve", manager_id, approval_version=version)
        reject_url = generate_approval_url("carryover-payout", item_id, "reject", manager_id, approval_version=version)
        html = tr.render_carryover_payout_approval_email(
            item_id, request_type, submitter_name, days,
            current_vacation, current_carryover, current_payout,
            new_vacation, new_carryover, new_payout, approve_url, reject_url,
        )
        sms_body = (
            f"{request_type} Request #{item_id} for {submitter_name} ({days} days).\n"
            f"If approved: Vac: {new_vacation}, CO: {new_carryover}, PO: {new_payout}.\n"
            f'Reply "CO Approve {item_id}" or "CO Reject {item_id}"'
        )
        return f"{request_type} Request #{item_id} Submitted by {submitter_name}", html, sms_body
    return make


def _build_receipt_content(record: RequestRecord, emp_fields: dict, fields: dict) -> tuple[str, str]:
    """The employee's "request received" receipt subject and HTML, kind by kind.

    Args:
        record: The request row.
        emp_fields: The submitter's staff fields.
        fields: The request in the SharePoint list-item shape.

    Returns:
        ``(subject, html)``.
    """
    submitter_name = emp_fields.get("Title", "")
    if record.kind == "leave":
        leave_type = record.leave_type or ""
        days = float(record.days or 0)
        start, end = record.start_date, record.end_date or record.start_date
        is_next = bool(start and end and is_next_year_request(start, end))
        projected = (
            simulate_leave_impact(emp_fields, leave_type, days, is_next)
            if leave_costs_balance(emp_fields, leave_type) else None
        )
        html = tr.render_leave_confirmation(fields, emp_fields, projected, conflict_warning=None)
        return f"Leave Request Received - {submitter_name}", html
    if record.kind == "overtime":
        projected = simulate_overtime_impact(emp_fields, float(record.hours or 0))
        html = tr.render_overtime_confirmation(fields, emp_fields, projected, conflict_warning=None)
        return f"Time Make-Up Request Received - {submitter_name}", html
    # carry-over / payout
    days = float(record.days or 0)
    request_type = OPTION_WORD.get(record.request_option or "", "Carry Over")
    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)
    new_vacation = current_vacation - days
    if request_type == "Carry Over":
        html = tr.render_carryover_confirmation(
            record.item_id, emp_fields, days, new_vacation, current_carryover + days, current_payout,
        )
        return "Request Received for Carry Over", html
    html = tr.render_payout_confirmation(
        record.item_id, emp_fields, days, new_vacation, current_carryover, current_payout + days,
    )
    return "Request Received for Payout", html


def _leave_sms_dates(start: date | None, end: date | None) -> str:
    """The date line of a leave approval text, matching the live email path.

    Args:
        start: The first day, or None.
        end: The last day, or None.

    Returns:
        A one-line date string ending in a newline, or "" when there is no date.
    """
    end = end or start
    if start and end:
        if start == end:
            return f"{start.strftime('%b %d, %Y')}\n"             # one day
        return f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}\n"
    if start:
        return f"{start.isoformat()}\n"
    return ""


def _leave_sms_balance(projected, emp_fields, leave_type, below_zero) -> str:
    """The balance line of a leave approval text, matching the live email path.

    Args:
        projected: The projected balances, or None when nothing moves.
        emp_fields: The submitter's staff fields (for the hourly/salaried split).
        leave_type: The leave type.
        below_zero: The floor warning when approval goes below zero, or None.

    Returns:
        The balance line (and a warning line when below zero), ending in a newline.
    """
    if projected and is_hourly(emp_fields):
        line = f"If approved: Sick: {projected['CurrentSickDayBalance']:g}.\n"   # hourly have one balance
    elif projected:
        line = (
            f"If approved: Vac: {projected['CurrentVacationBalance']}, "
            f"Sick: {projected['CurrentSickDayBalance']}, "
            f"MU: {projected['CurrentOvertimeBalance']}, "
            f"CO: {projected['CarryOver']}.\n"
        )
    elif is_hourly(emp_fields) and leave_type == "Vacation":
        line = "Counted as vacation taken, no balance used.\n"
    else:
        line = "No balance change.\n"
    if below_zero:                                                 # texts carry no colour: say it plainly
        line += f"WARNING: {floor_warning_text(below_zero)}\n"
    return line


async def _do_submit(session, kind, item_id, channel, notify, actor) -> bool:
    """Record one request's submission in the given session (no commit).

    Args:
        session: The open session the caller commits.
        kind: The request kind.
        item_id: The item_id.
        channel: The submitted_channel, or None to take it from RequestSource.
        notify: True also queues the manager email and text and the receipt;
            False (a sick day approved on the spot) records only the event.
        actor: Who submitted, for the event; None falls back to the employee.

    Returns:
        True when a row was found and recorded; False when there is no such row.
    """
    record = await _load(session, kind, item_id)
    if record is None:
        return False                                              # gone: nothing to record
    fields = TypedRequestList(kind)._item_shape(record)["fields"]  # SharePoint shape for the resolve fallback
    employee = await _resolve_employee(record, fields)
    managers = await get_all_managers_for_employee(employee) if employee else []
    manager = managers[0] if managers else None
    _set_identity(record, employee, manager, channel or _channel_from_source(record))

    version = record.approval_version
    submitted_key = step_key(kind, record.item_id, "submitted", version)
    event = await append_event(
        session, record, "submitted", record.submitted_channel or "system",
        actor or record.employee_name, {}, submitted_key,
    )
    log_step(record, "submitted", channel=record.submitted_channel, notify=notify)

    if notify and employee is not None and managers:              # queue the manager ask and the receipt
        await _enqueue_notifications(session, record, employee, managers, event=event)
    elif notify and employee is not None:                        # no manager resolved: send the receipt only
        logger.warning("No manager for %s #%s; receipt queued, manager not asked", kind, record.item_id)
        emp_fields = employee.get("fields", {})
        if record.employee_email:
            subject, html = _build_receipt_content(record, emp_fields, fields)
            await enqueue_message(
                session, record, event, "receipt", "email", record.employee_email, subject, html,
                f"{step_key(kind, record.item_id, 'receipt_sent', version)}:email:{record.employee_email}",
            )
    return True


async def submit(kind, item_id, *, channel=None, notify=True, actor=None, session=None) -> bool:
    """Finish a new request: record its identity, the submitted event, and queue messages.

    Opens its own transaction and wakes the outbox sender when ``session`` is not
    given; when it is (the SharePoint check shares one), it writes into that
    session and leaves the commit and the wake to the caller.

    Args:
        kind: The request kind ("leave", "overtime", "carryover_payout").
        item_id: The item_id people and links use.
        channel: The submitted_channel, or None to take it from RequestSource.
        notify: True queues the manager approval email and text and the receipt;
            False records only the submitted event (a sick day approved at once).
        actor: Who submitted (for the event), or None to use the employee name.
        session: An open session to write into, or None to open one and commit.

    Returns:
        True when the request was found and recorded; False when it was not.
    """
    if session is not None:
        return await _do_submit(session, kind, item_id, channel, notify, actor)
    async with async_session() as own:
        recorded = await _do_submit(own, kind, item_id, channel, notify, actor)
        await own.commit()
    if recorded:
        _wake_sender()                                            # deliver the queued messages without waiting
    return recorded


async def queue_reminder(kind, item_id, *, today=None, session=None) -> bool:
    """Queue a manager-approval reminder for one pending request, once a day.

    The reminder re-asks the manager with fresh links (email only, as the live
    path does). Its outbox key carries today's date, so the same request is never
    reminded twice on the same day, across reruns and instances.

    Args:
        kind: The request kind.
        item_id: The item_id.
        today: The date for the key, or None for today (lets a test move the day).
        session: An open session to write into, or None to open one and commit.

    Returns:
        True when a reminder was queued; False when the request is gone or not
        pending.
    """
    if session is not None:
        return await _queue_reminder(session, kind, item_id, today)
    async with async_session() as own:
        queued = await _queue_reminder(own, kind, item_id, today)
        await own.commit()
    if queued:
        _wake_sender()
    return queued


async def _queue_reminder(session, kind, item_id, today) -> bool:
    """Queue a reminder in the given session (no commit). See ``queue_reminder``."""
    record = await _load(session, kind, item_id)
    if record is None or record.status != "pending":
        return False                                              # gone or already decided: nothing to remind
    employee = await _resolve_employee(record, TypedRequestList(kind)._item_shape(record)["fields"])
    managers = await get_all_managers_for_employee(employee) if employee else []
    if not managers:
        return False                                              # nobody to remind
    from app.models.mixins import utcnow
    suffix = (today or date.today()).isoformat()
    await _enqueue_notifications(session, record, employee, managers, event=None, suffix=suffix)
    record.reminder_count = (record.reminder_count or 0) + 1      # bookkeeping for the cadence
    record.last_reminder_at = utcnow()
    log_step(record, "reminder_sent", suffix=suffix)
    return True


# How long a pending request waits for its first reminder, then each repeat.
FIRST_REMINDER_DAYS = 30
REPEAT_REMINDER_DAYS = 7


def _reminder_due(record: RequestRecord, now) -> bool:
    """Whether a pending request is due a reminder now, by the usual cadence.

    First reminder after ``FIRST_REMINDER_DAYS`` from submission, then one every
    ``REPEAT_REMINDER_DAYS`` from the last. A request past its start date is left
    for the caller to skip; this only times the cadence.

    Args:
        record: The pending request row.
        now: The current time (aware), for the comparison.

    Returns:
        True when a reminder is due.
    """
    from datetime import timedelta
    last = record.last_reminder_at or record.created_at
    if last is None:
        return True                                              # no clock to measure from: due
    if last.tzinfo is None:                                      # SQLite hands back naive UTC
        from datetime import timezone
        last = last.replace(tzinfo=timezone.utc)
    threshold = FIRST_REMINDER_DAYS if (record.reminder_count or 0) == 0 else REPEAT_REMINDER_DAYS
    return now - last >= timedelta(days=threshold)


async def queue_due_reminders() -> int:
    """Queue a reminder for every pending Postgres request whose cadence is due.

    The scheduled reminder task calls this once requests and staff are in Postgres
    instead of its SharePoint sweep. Each request still reminds at most once a day
    (the dated outbox key), and a request past its start date is skipped.

    Returns:
        How many requests had a reminder queued this run.
    """
    from app.models.mixins import utcnow
    now = utcnow()
    today = date.today()
    async with async_session() as session:
        pending = (await session.execute(
            select(RequestRecord).where(RequestRecord.status == "pending")
        )).scalars().all()
        queued = 0
        for record in pending:
            if record.start_date and record.start_date < today:
                continue                                          # the date has passed: let it lapse
            if not _reminder_due(record, now):
                continue
            if await _queue_reminder(session, record.kind, record.item_id, today):
                queued += 1
        await session.commit()
    if queued:
        _wake_sender()
    return queued
