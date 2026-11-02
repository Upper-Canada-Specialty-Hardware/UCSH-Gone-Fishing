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
on staff (the same path), and a manager logging a sick day (which is approved on
the spot, so it takes only the ``submitted`` event, ``notify`` False). Each write
carries a ``step_key`` that is unique in its table, so a second call with the
same keys adds nothing.
"""
import logging
from datetime import date

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.request_record import RequestRecord
from app.repositories.postgres.staff import is_archived  # tells an archived staff record from an active one (#119)
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.approval_links import generate_approval_url
from app.services.app_settings import toronto_today  # Toronto calendar day for the booking policy (#407)
from app.services.balance import (
    carryover_available_for,
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
from app.services.request_journal import append_event, enqueue_message, log_step, record_change, step_key
from app.services.request_states import can_decide, move
from app.services.request_submitter import resolve_request_submitter
from app.services.vacation_cover import choose_available_manager, today_toronto
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


def _assign_manager(record: RequestRecord, manager: dict | None) -> None:
    """Point the request at the manager who was actually asked (the stand-in) (#369).

    When the first manager on the list is off, the ask goes to the next available
    one, who becomes the request's assigned manager, so the dashboards and the
    result email (#371) name whoever really has it. Leaves the assignment as it
    was when no manager is available (everyone is off).

    Args:
        record: The request row to update in place.
        manager: The manager now holding the request, or None to leave it as is.
    """
    if manager is None:
        return                                                      # everyone off: keep the nominal assignment
    record.manager_id = str(manager["id"])                          # the manager actually asked
    record.manager_name = (manager.get("fields") or {}).get("Title") or record.manager_name


async def _record_reroute(session, record: RequestRecord, chosen: dict | None,
                          skipped: list[dict], channel: str, event_suffix: str) -> None:
    """Record that the approval ask was routed past off managers to a stand-in (#369).

    Adds a ``sent_to_manager`` timeline event naming the manager asked and the off
    managers skipped over (each with the date they are back), so the request's
    timeline shows the move. Recorded only when a later manager stood in for off
    earlier ones; a plain first-manager ask and an all-off request add nothing
    here (an all-off request is surfaced by the admin cover list instead).

    Args:
        session: The open session the caller commits.
        record: The rerouted request.
        chosen: The manager the ask went to, or None when everyone is off.
        skipped: The off managers passed over (from ``choose_available_manager``).
        channel: Where the routing happened (the submission or reminder channel).
        event_suffix: A tag that keeps the event's key unique per routing moment
            (the submission, an edit's version, or the reminder's date).
    """
    if chosen is None or not skipped:
        return                                                      # no stand-in happened: nothing to record
    key = step_key(record.kind, record.item_id, "sent_to_manager", record.approval_version,
                   suffix=f"reroute:{event_suffix}")
    await append_event(
        session, record, "sent_to_manager", channel, None,
        {"manager": record.manager_name, "skipped": skipped}, key,
    )


async def _enqueue_notifications(session, record, employee, managers, *, event, suffix=None,
                                 include_receipt=True, reask_suffix=None) -> None:
    """Queue the manager approval email and text and (optionally) the employee receipt.

    One manager email (and a text when they have a cell) per manager, then one
    receipt to the employee. With a ``suffix`` (a date) and the reminder step the
    same rows become a once-a-day reminder instead, email only, its subject
    prefixed "Reminder: " (#422). An admin edit asks the manager again with fresh
    links but sends no receipt (``include_receipt`` False). A ``reask_suffix``
    makes a fresh manager ask (a reprocess) key-unique so every press sends and
    both channels go out (#421). Each row carries a step_key that is unique in the
    outbox, so a repeat queues nothing again.

    Args:
        session: The open session; not committed here.
        record: The request row.
        employee: The submitter's staff record.
        managers: The managers to ask (first is primary).
        event: The event the messages follow, for the outbox link, or None.
        suffix: A date string for a reminder (its key is per-day); None for a
            first submission, which also sends the receipt.
        include_receipt: False skips the employee receipt (an edit re-ask only).
        reask_suffix: A tag that makes a non-reminder manager ask key-unique, so
            an admin reprocess sends every press; None for the once-per-version ask.
    """
    emp_fields = employee.get("fields", {})
    fields = TypedRequestList(record.kind)._item_shape(record)["fields"]   # SharePoint shape the templates read
    version = record.approval_version
    is_reminder = suffix is not None
    message_type = "reminder" if is_reminder else "manager_approval"
    # The step the key is built on: a dated reminder step, or the sent-to-manager
    # step (with an optional reprocess suffix so every reprocess press is unique).
    manager_step = (
        step_key(record.kind, record.item_id, "reminder_sent", version, suffix=suffix)
        if is_reminder else
        step_key(record.kind, record.item_id, "sent_to_manager", version, suffix=reask_suffix)
    )
    built = _build_manager_content(record, emp_fields, fields)      # per-kind subject + bodies

    for manager in managers:
        mgr_fields = manager.get("fields", {})
        manager_id = manager["id"]
        mgr_email = (mgr_fields.get("EmailAddress") or "").strip()
        subject, html, sms_body = built(manager_id)                # links are per manager
        if is_reminder:
            subject = f"Reminder: {subject}"                       # match the SharePoint path's reminder subject (#422)
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

    if is_reminder or not include_receipt:
        return                                                      # a reminder or an edit re-asks the manager only
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
        last_day = record.end_date or record.start_date
        carryover_avail = carryover_available_for(last_day, toronto_today())   # carry-over expired for these dates? (#407)
        projected = (
            simulate_leave_impact(emp_fields, leave_type, days, carryover_avail)
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
        last_day = record.end_date or record.start_date
        carryover_avail = carryover_available_for(last_day, toronto_today())   # carry-over expired for these dates? (#407)
        projected = (
            simulate_leave_impact(emp_fields, leave_type, days, carryover_avail)
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


# Payout days are capped at this many across all of a person's payout requests.
PAYOUT_CAP_DAYS = 5


async def _submit_time_rejection(record: RequestRecord, emp_fields: dict, fields: dict):
    """Whether a new request is auto-rejected at submit, and the notice to send.

    Mirrors, for the Postgres path, the auto-rejections the SharePoint intake runs
    before a manager is asked: overtime worked on a company holiday; a partial-day
    leave on a holiday or over the half-Friday limit; a payout past the cap; and a
    carry-over/payout by hourly staff or one that would take vacation below zero.
    Pure of writes: it only reads the request and the staff balances and renders
    the employee's rejection email.

    Args:
        record: The request row (identity and details already stamped).
        emp_fields: The submitter's Staff Directory fields (location, balances).
        fields: The request in the SharePoint list-item shape, for the templates.

    Returns:
        ``(reason, subject, html)`` when the request is rejected, or None to let
        it go to the manager.
    """
    if record.kind == "carryover_payout":
        return _carryover_submit_rejection(record, emp_fields)    # no holiday calendar needed
    # Only partial-day leave is auto-rejected at submit; full-day leave is not.
    if record.kind == "leave" and (record.leave_type or "") != "Half Day or Partial Day Off":
        return None
    start = record.start_date
    if start is None:                                             # no date to test against the calendar
        return None
    # The employee's province decides which holidays apply; an unmapped location
    # degrades to "no holidays" rather than failing the whole submission.
    from app.services.employee import map_location_to_province
    from app.services.holidays import (
        get_half_friday_season,
        get_holidays_for_province,
        is_company_holiday,
        is_half_friday,
    )
    try:
        province = map_location_to_province(emp_fields.get("Location", ""))
    except ValueError:
        province = None
    holidays = await get_holidays_for_province(province) if province else []
    is_holiday, holiday_name = is_company_holiday(start, holidays)

    if record.kind == "overtime":
        if is_holiday:
            reason = f"{start} is the company holiday {holiday_name}."
            html = tr.render_overtime_auto_rejected(fields, holiday_name, reason)
            return reason, "Overtime Request - Auto Rejected", html
        return None

    # partial-day leave
    days = float(record.days or 0)
    if is_holiday:
        reason = f"{start} is the company holiday {holiday_name}."
        html = tr.render_partial_day_holiday_rejected(fields, holiday_name, reason)
        return reason, "Partial Day Request - Auto Rejected", html
    if is_half_friday(start, get_half_friday_season(holidays)) and days > 0.5:
        reason = f"Requested {days} days on the half-Friday {start}, which exceeds the 0.5 day limit."
        html = tr.render_partial_day_halffriday_rejected(fields, reason)
        return reason, f"Leave Request {record.item_id} {fields.get('Title', '')}", html
    return None


def _carryover_submit_rejection(record: RequestRecord, emp_fields: dict):
    """The carry-over/payout submit-time auto-rejection, if any (pure, no I/O).

    Args:
        record: The carry-over/payout request row.
        emp_fields: The submitter's Staff Directory balance fields.

    Returns:
        ``(reason, subject, html)`` when rejected, or None.
    """
    days = float(record.days or 0)
    request_type = OPTION_WORD.get(record.request_option or "", "Carry Over")
    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)
    if request_type == "Payout" and current_payout + days > PAYOUT_CAP_DAYS:
        new_payout = current_payout + days
        reason = f"Payout cap exceeded - new total would be {new_payout} days (max {PAYOUT_CAP_DAYS})."
        html = tr.render_payout_cap_rejected(record.item_id, emp_fields, reason)
        return reason, "Payout Request - Auto Rejected", html
    # Vacation cannot go below zero; hourly staff have no vacation pot at all (#300).
    new_vacation = current_vacation - days
    hourly = is_hourly(emp_fields)
    if hourly or new_vacation < 0:
        reason = (
            "Hourly staff have no vacation balance to carry over or pay out."
            if hourly else
            f"Vacation balance would go to {new_vacation} days ({current_vacation} - {days})."
        )
        html = tr.render_system_override_reject(
            record.item_id, emp_fields, request_type,
            current_vacation, current_carryover, current_payout, reason,
        )
        return reason, "Carry Over / Payout Request - Auto Rejected", html
    return None


def _mark_auto_rejected(record: RequestRecord) -> None:
    """Stamp a request as system-rejected at submit, matching the SharePoint flags.

    Args:
        record: The request row to mark in place.
    """
    from app.models.mixins import utcnow
    record.status = "rejected"                                    # terminal: no manager is asked
    move(record, "rejected", reason="auto-rejected at submit")    # getting_ready -> rejected (#455)
    record.decided_at = utcnow()
    record.decided_by = "system"
    record.decided_channel = record.submitted_channel or "system"
    # Keep the SharePoint-compat flags older reads check, as the decision path does.
    extra = dict(record.extra or {})
    if record.kind == "leave":
        extra["ApproveProcessedFlag"] = "Processed"
    elif record.kind == "carryover_payout":
        extra["SystemState"] = "Processed"
    record.extra = extra


async def _auto_reject(session, record: RequestRecord, rejection) -> None:
    """Record a submit-time auto-rejection and queue the employee's notice (no commit).

    Marks the request rejected, appends a ``rejected`` event and queues the
    rejection email through the outbox. Nothing goes to the manager. Every write
    is keyed, so a rerun of the same submission adds nothing.

    Args:
        session: The open session the caller commits.
        record: The request row being rejected.
        rejection: ``(reason, subject, html)`` from ``_submit_time_rejection``.
    """
    reason, subject, html = rejection
    _mark_auto_rejected(record)
    version = record.approval_version
    rejected_key = step_key(record.kind, record.item_id, "rejected", version)
    event = await append_event(
        session, record, "rejected", record.submitted_channel or "system", "system",
        {"reason": reason}, rejected_key,
    )
    if record.employee_email:                                     # tell the employee why, through the outbox
        await enqueue_message(
            session, record, event, "rejection", "email", record.employee_email, subject, html,
            f"{rejected_key}:email:{record.employee_email}",
        )
    log_step(record, "rejected", channel=record.submitted_channel, auto=True)


async def _queue_bereavement_alert(session, record: RequestRecord, employee: dict | None,
                                   fields: dict, event) -> None:
    """Queue the HR alert for a bereavement or jury-duty leave, once per request (#427).

    Mirrors the SharePoint path's ``send_bereavement_alert``: the same recipients,
    subject and template, but through the outbox so it is retried, rehearsal-gated
    and idempotent like every other Postgres message. One row per HR recipient,
    each keyed once per request, so a rerun of the submission queues nothing again.
    Does nothing for any other leave type or request kind. The submitter name is
    taken from the staff record when one resolved, else from the row, so an
    admin-created leave with no resolved employee still alerts HR (finding 4).

    Args:
        session: The open session the caller commits.
        record: The request row (identity already stamped).
        employee: The submitter's staff record, or None when none resolved.
        fields: The request in the SharePoint list-item shape, for the template.
        event: The submitted event, for the outbox link, or None.
    """
    from app.services.leave_requests import (
        BEREAVEMENT_ALERT_LEAVE_TYPES,
        BEREAVEMENT_ALERT_RECIPIENTS,
        BEREAVEMENT_ALERT_SUBJECT,
    )
    if record.kind != "leave" or (record.leave_type or "") not in BEREAVEMENT_ALERT_LEAVE_TYPES:
        return                                                    # only bereavement / jury duty alert HR
    # Name from the staff record first, then the row: an alert may fire before an
    # employee is resolved, and the template reads the name from here.
    submitter_name = (employee or {}).get("fields", {}).get("Title", "") or (record.employee_name or "")
    html = tr.render_bereavement_alert(fields, submitter_name)    # same template the SharePoint path uses
    alert_step = step_key(record.kind, record.item_id, "bereavement_alert", record.approval_version)
    for recipient in BEREAVEMENT_ALERT_RECIPIENTS:                # one outbox row per HR recipient
        await enqueue_message(
            session, record, event, "bereavement_alert", "email", recipient,
            BEREAVEMENT_ALERT_SUBJECT, html, f"{alert_step}:email:{recipient}",
        )


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
    # Route past any first managers who are off today: the ask goes to the first
    # manager not on approved full-day leave, who becomes the assigned manager
    # (#369). When everyone is off, no one is asked and the first manager stays
    # the nominal assignee (the admin cover list surfaces it, #370).
    chosen, skipped = (
        await choose_available_manager(session, managers, today_toronto()) if managers else (None, [])
    )
    assigned = chosen or (managers[0] if managers else None)
    _set_identity(record, employee, assigned, channel or _channel_from_source(record))

    version = record.approval_version
    submitted_key = step_key(kind, record.item_id, "submitted", version)
    event = await append_event(
        session, record, "submitted", record.submitted_channel or "system",
        actor or record.employee_name, {}, submitted_key,
    )
    log_step(record, "submitted", channel=record.submitted_channel, notify=notify)

    # A bereavement or jury-duty leave tells HR as soon as the request is recorded,
    # BEFORE the notify / non-pending returns below. An admin-created Approved
    # bereavement leave arrives with notify False and status "approved", and the
    # SharePoint-path send now no-ops in Postgres mode, so queuing it only on the
    # notify path left HR never told (finding 4). It is keyed once per request and
    # works with or without a resolved employee; it no-ops for any other type.
    alert_fields = TypedRequestList(kind)._item_shape(record)["fields"]   # name now stamped on the row
    await _queue_bereavement_alert(session, record, employee, alert_fields, event)

    if not notify:
        # A sick day or admin-created request approved on the spot: the request is
        # prepared (identity and manager stamped above), so it is decidable now and
        # the caller approves it in the same request handler, with no race gap. An
        # unresolved submitter stays getting_ready, so it cannot be decided (#455).
        if employee is not None:
            move(record, "waiting_on_approval", reason="submitted, approved on the spot")
        return True
    if record.status != "pending":
        return True                                               # already decided (a rerun or a parallel action)

    # Rebuild the SharePoint-shaped fields now the identity is stamped, so the
    # emails and any rejection notice carry the submitter's name and details.
    fields = TypedRequestList(kind)._item_shape(record)["fields"]

    if employee is not None:
        # The same auto-rejections the SharePoint intake runs (overtime on a
        # holiday, the payout cap, hourly carry-over/payout, partial-day limits)
        # must still fire here, before the manager is ever asked (#324).
        rejection = await _submit_time_rejection(record, employee.get("fields", {}), fields)
        if rejection is not None:
            await _auto_reject(session, record, rejection)        # rejected event + employee notice, no manager ask
            return True

    if employee is not None and managers:                         # queue the manager ask and the receipt
        # Only the chosen manager is asked; an all-off request passes [] here, so
        # _enqueue_notifications still sends the receipt but no manager email.
        await _enqueue_notifications(session, record, employee, [chosen] if chosen else [], event=event)
        await _record_reroute(session, record, chosen, skipped,
                              record.submitted_channel or "system", "submit")
    elif employee is not None:                                    # no manager resolved: send the receipt only
        logger.warning("No manager for %s #%s; receipt queued, manager not asked", kind, record.item_id)
        emp_fields = employee.get("fields", {})
        if record.employee_email:
            subject, html = _build_receipt_content(record, emp_fields, fields)
            await enqueue_message(
                session, record, event, "receipt", "email", record.employee_email, subject, html,
                f"{step_key(kind, record.item_id, 'receipt_sent', version)}:email:{record.employee_email}",
            )
    if employee is not None:
        # Submit finished and the request is complete (a manager was asked, or none
        # was found but the request is otherwise whole): it is now decidable (#455).
        # An unresolved submitter leaves the row getting_ready, so it cannot be
        # decided and the stuck list surfaces it.
        move(record, "waiting_on_approval", reason="submit finished")
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


async def reprocess_request(kind, item_id, reason, *, actor="admin",
                            channel="admin_dashboard", session=None) -> bool:
    """Re-ask the available manager for a pending request, email AND text (#421).

    An admin reprocess is not a reminder: it must send on every press, over both
    channels, with fresh links, and it must not advance the reminder cadence. It
    routes to the first available manager exactly as a reminder does
    (``choose_available_manager``), records its own ``sent_to_manager`` timeline
    step carrying the admin's reason, and keys every row with a per-press suffix
    so a second press sends again. ``reminder_count`` and ``last_reminder_at`` are
    left untouched.

    Args:
        kind: The request kind ("leave", "overtime", "carryover_payout").
        item_id: The item_id people and links use.
        reason: Why the admin reprocessed, recorded on the timeline step.
        actor: Who reprocessed, for the event.
        channel: Where the reprocess came from (a request_vocab channel).
        session: An open session to write into, or None to open one and commit.

    Returns:
        True when a manager was asked; False when the request is gone, not
        pending, or every manager is off (nobody to ask).
    """
    if session is not None:
        return await _reprocess_request(session, kind, item_id, reason, actor, channel)
    async with async_session() as own:
        asked = await _reprocess_request(own, kind, item_id, reason, actor, channel)
        await own.commit()
    if asked:
        _wake_sender()
    return asked


async def _reprocess_request(session, kind, item_id, reason, actor, channel) -> bool:
    """Re-ask the available manager for a pending request in the given session (no commit).

    The body of ``reprocess_request`` (see it for the full behaviour): it routes to
    the first available manager, records one ``sent_to_manager`` timeline step
    carrying the admin's reason, and queues a fresh email AND text with a per-press
    key so every press sends, without advancing the reminder cadence. The caller
    opens the session, commits, and wakes the sender.

    Args:
        session: The open session the caller commits.
        kind: The request kind ("leave", "overtime", "carryover_payout").
        item_id: The item_id people and links use.
        reason: Why the admin reprocessed, recorded on the timeline step.
        actor: Who reprocessed, for the event.
        channel: Where the reprocess came from (a request_vocab channel).

    Returns:
        True when a manager was asked; False when the request is gone, no longer
        pending, its submitter/managers cannot be resolved, or every manager is
        off today (nobody to ask), in which case nothing is queued.
    """
    from app.models.mixins import utcnow
    record = await _load(session, kind, item_id)
    if record is None or record.status != "pending":
        return False                                              # gone or already decided: nothing to re-ask
    employee = await _resolve_employee(record, TypedRequestList(kind)._item_shape(record)["fields"])
    if employee is None:
        return False                                              # submitter cannot be resolved: nobody to ask
    managers = await get_all_managers_for_employee(employee)
    if not managers:
        # A resolved submitter with no manager at all: mirror _do_submit, which
        # makes such a request waiting_on_approval (decidable, carried on the admin
        # cover list) rather than leaving it stuck. No manager exists to email, so
        # nothing is sent, but the state is finished: needs_attention /
        # getting_ready -> waiting_on_approval (#455); an already-waiting row is
        # unchanged. Returning True lets the admin reprocess resolve a stuck row.
        move(record, "waiting_on_approval", reason="reprocessed, no manager to ask")
        return True
    # Route to the first available manager, the same rule reminders use (#369).
    chosen, skipped = await choose_available_manager(session, managers, today_toronto())
    if chosen is None:
        return False                                              # everyone off: nothing to send this press
    _assign_manager(record, chosen)                               # the request follows whoever is asked
    # A per-press tag makes every reprocess key-unique, so a second press the same
    # day still sends (a reminder's per-day key would swallow it); utcnow carries
    # microseconds, so two presses never collide.
    press = f"reprocess:{_naive_utc(utcnow()).isoformat()}"
    reprocess_key = step_key(kind, record.item_id, "sent_to_manager", record.approval_version, suffix=press)
    # One timeline step per press, carrying the admin's reason (and who was skipped).
    event = await append_event(
        session, record, "sent_to_manager", channel, actor,
        {"manager": record.manager_name, "reason": reason, "reprocess": True, "skipped": skipped},
        reprocess_key,
    )
    # Both channels, fresh links, no receipt, no "Reminder:" prefix; the unique
    # suffix keeps each press's outbox rows distinct.
    await _enqueue_notifications(
        session, record, employee, [chosen], event=event, include_receipt=False, reask_suffix=press,
    )
    # Processing again makes a stuck request decidable: needs_attention or
    # getting_ready -> waiting_on_approval (#455). Already-waiting rows are unchanged.
    move(record, "waiting_on_approval", reason="reprocessed")
    log_step(record, "sent_to_manager", reprocess=True, actor=actor)
    return True                                                   # reminder_count / last_reminder_at untouched


async def _queue_reminder(session, kind, item_id, today) -> bool:
    """Queue a reminder in the given session (no commit). See ``queue_reminder``."""
    record = await _load(session, kind, item_id)
    if record is None or record.status != "pending":
        return False                                              # gone or already decided: nothing to remind
    if not can_decide(record):
        return False                                              # getting_ready / needs_attention: never remind (#455)
    employee = await _resolve_employee(record, TypedRequestList(kind)._item_shape(record)["fields"])
    # An archived submitter (#119) is off every approval route: no manager is
    # reminded, and the request simply stays pending and visible to admins (#368).
    if employee is not None and is_archived(employee.get("fields", {})):
        return False
    managers = await get_all_managers_for_employee(employee) if employee else []
    if not managers:
        return False                                              # nobody to remind
    from app.models.mixins import utcnow
    on_date = today or today_toronto()                            # Toronto day, matching submit routing (#369)
    # Re-decide on the reminder's own day: a request moves on when its manager
    # leaves and goes back to them when they return (#369). The reminder goes to
    # whoever is available.
    chosen, skipped = await choose_available_manager(session, managers, on_date)
    if chosen is None:
        # Everyone is still off: nothing is sent, so the cadence must not advance
        # (no count bump, no reminder_sent log). The request stays on the admin
        # cover list and the next sweep tries again (#370).
        return False
    _assign_manager(record, chosen)                               # the request follows whoever is asked
    suffix = on_date.isoformat()
    await _enqueue_notifications(session, record, employee, [chosen], event=None, suffix=suffix)
    await _record_reroute(session, record, chosen, skipped, "system", suffix)
    record.reminder_count = (record.reminder_count or 0) + 1      # bookkeeping for the cadence
    record.last_reminder_at = utcnow()
    log_step(record, "reminder_sent", suffix=suffix)
    return True


# How long a pending request waits for its first reminder, then each repeat.
FIRST_REMINDER_DAYS = 30
REPEAT_REMINDER_DAYS = 7
# Carry-over/payout requests have no event date to stop at, so reminders stop
# after this many, matching reminders.py's MAX_REMINDERS_WITHOUT_DATE (finding 12).
MAX_REMINDERS_WITHOUT_DATE = 6


def _reminder_cutoff_passed(record: RequestRecord, today: date, count: int | None = None) -> bool:
    """Whether a pending request is moot and reminders should stop (finding 12).

    Mirrors reminders.py's SharePoint cutoff so the Postgres path stops in the
    same cases: a leave or overtime once its start date has passed, and a
    carry-over/payout (which has no event date) after year-end of the year it was
    made in, or after ``MAX_REMINDERS_WITHOUT_DATE`` reminders.

    Args:
        record: The pending request row.
        today: Today's date in the local sense the caller uses.
        count: Reminders already sent, from ``_reminder_clock``; None reads the
            row's own count.

    Returns:
        True when reminders should stop for this request.
    """
    if record.kind in ("leave", "overtime"):
        return record.start_date is not None and record.start_date < today   # the date is in the past
    if record.kind == "carryover_payout":
        created = record.created_at
        if created is not None and today.year > created.year:                # past year-end of its year
            return True
        sent = (record.reminder_count or 0) if count is None else count       # merged count when given
        return sent >= MAX_REMINDERS_WITHOUT_DATE                            # or capped by count
    return False


def _reminder_clock(record: RequestRecord, state) -> tuple:
    """Where a request's reminder cadence stands: the last email time and the count.

    A request made in the app keeps its own clock on the row. A request imported
    from SharePoint carries no reminder history on the row, because the old path
    kept it in ``request_approval_state`` (when the approval email or the last
    reminder went out, and how many reminders were sent). Reading that history
    keeps the cadence where it was at the switch, so the first sweep after the
    switch does not remind every old pending request at once.

    Args:
        record: The pending request row.
        state: Its ``request_approval_state`` row, or None.

    Returns:
        ``(last, count)``: the time the cadence is measured from (None when there
        is nothing to measure from) and the reminders already sent.
    """
    last = record.last_reminder_at or record.created_at          # an app request's own clock
    count = record.reminder_count or 0
    if record.sp_item_id is not None and state is not None:      # imported: continue the old clock
        emailed = state.last_emailed_at                            # approval email or last old-path reminder
        if emailed is not None and (record.last_reminder_at is None or emailed > _naive_utc(record.last_reminder_at)):
            last = emailed                                         # the later of the two clocks wins
        count = max(count, state.reminder_count or 0)              # reminders sent on either path
    return last, count


def _naive_utc(moment):
    """Drop the timezone from an aware UTC time, so it compares with a naive one.

    Args:
        moment: A datetime, aware or naive (naive means UTC here).

    Returns:
        The same moment as a naive UTC datetime.
    """
    from datetime import timezone
    if moment.tzinfo is None:
        return moment                                              # already naive UTC
    return moment.astimezone(timezone.utc).replace(tzinfo=None)    # aware -> naive UTC


def _reminder_due(record: RequestRecord, now, clock: tuple | None = None) -> bool:
    """Whether a pending request is due a reminder now, by the usual cadence.

    First reminder after ``FIRST_REMINDER_DAYS`` from submission (or, for an
    imported request, from its approval email), then one every
    ``REPEAT_REMINDER_DAYS`` from the last. A request past its start date is left
    for the caller to skip; this only times the cadence.

    Args:
        record: The pending request row.
        now: The current time (aware), for the comparison.
        clock: ``(last, count)`` from ``_reminder_clock``; None reads the row alone.

    Returns:
        True when a reminder is due.
    """
    from datetime import timedelta
    last, count = clock if clock is not None else (
        record.last_reminder_at or record.created_at, record.reminder_count or 0,
    )
    if last is None:
        return True                                              # no clock to measure from: due
    if last.tzinfo is None:                                      # SQLite hands back naive UTC
        from datetime import timezone
        last = last.replace(tzinfo=timezone.utc)
    threshold = FIRST_REMINDER_DAYS if count == 0 else REPEAT_REMINDER_DAYS   # first wait, then the repeat
    return now - last >= timedelta(days=threshold)


async def _manager_ever_asked(session, record: RequestRecord) -> bool:
    """Whether a manager-approval message was ever queued for this request's version.

    A request submitted while every manager was off has no manager asked, so no
    ``manager_approval`` outbox row exists for its current approval version; it
    waits on the admin cover list until a manager is back (#370). This tells that
    case apart from a request already asked and merely due a reminder.

    An imported request (``sp_item_id`` set) was asked by the old SharePoint path,
    which left no Postgres outbox row, so it counts as asked here (the truly
    never-emailed imported ones are already filtered out of the sweep above). An
    app-native request is asked once its ``manager_approval`` outbox row exists,
    scoped to the current approval version, so a re-ask after an admin edit (a new
    version) is treated as never-asked again until its own ask goes out.

    Args:
        session: The open sweep session.
        record: The pending request row.

    Returns:
        True when the manager was asked (imported, or a manager_approval outbox
        row exists for the row's version).
    """
    if record.sp_item_id is not None:
        return True                                            # imported: the old path asked the manager
    from app.models.outbox import Outbox
    # The manager ask carries the version's sent_to_manager step key, with the
    # channel and recipient appended, so match on that prefix.
    prefix = step_key(record.kind, record.item_id, "sent_to_manager", record.approval_version)
    found = (await session.execute(
        select(Outbox.id).where(
            Outbox.request_id == record.id,
            Outbox.message_type == "manager_approval",
            Outbox.idempotency_key.like(f"{prefix}:%"),
        ).limit(1)
    )).first()
    return found is not None


async def _first_ask(session, record: RequestRecord, today: date) -> bool:
    """Ask the first available manager for an all-off request once one is back (#370).

    A request submitted while every manager was off is never sent to a manager and
    sits on the admin cover list. This makes the daily sweep ask the first manager
    who is available again, straight away rather than after the 30-day reminder
    wait, because the request was never asked at all. The ask reuses the version's
    ``sent_to_manager`` outbox key, so it happens once; the employee receipt
    already went out at submit, so none is sent here. The reminder clock is started
    from this ask, so the first real reminder follows the usual wait after it.

    Args:
        session: The open sweep session (not committed here).
        record: The pending, never-asked request row.
        today: Today in Toronto, for deciding which managers are off.

    Returns:
        True when a manager was asked; False when every manager is still off, or the
        submitter or managers cannot be resolved, or the submitter is archived,
        leaving the request on the admin cover list.
    """
    from app.models.mixins import utcnow
    if not can_decide(record):
        return False                                              # getting_ready / needs_attention: never ask (#455)
    employee = await _resolve_employee(record, TypedRequestList(record.kind)._item_shape(record)["fields"])
    # An archived submitter (#119) is off every approval route: never ask a manager.
    if employee is not None and is_archived(employee.get("fields", {})):
        return False
    managers = await get_all_managers_for_employee(employee) if employee else []
    if not employee or not managers:
        return False                                              # cannot resolve: leave it on cover
    chosen, skipped = await choose_available_manager(session, managers, today)
    if chosen is None:
        return False                                              # still everyone off: nothing to ask yet
    _assign_manager(record, chosen)                               # the request now points at who was asked
    # A genuine first ask, not a reminder: suffix None uses the version's
    # sent_to_manager key (so it happens once) and the manager_approval message,
    # and include_receipt False skips the receipt the employee already got.
    await _enqueue_notifications(
        session, record, employee, [chosen], event=None, include_receipt=False,
    )
    await _record_reroute(session, record, chosen, skipped, "system", "first-ask")
    record.last_reminder_at = utcnow()                            # start the reminder clock from this ask
    log_step(record, "sent_to_manager", late=True)
    return True


async def queue_due_reminders() -> int:
    """Queue a reminder for every pending Postgres request whose cadence is due.

    The scheduled reminder task calls this once requests and staff are in Postgres
    instead of its SharePoint sweep. It keeps the SharePoint path's stops exactly
    (finding 12): the carry-over/payout year-end and max-count cutoff, and the
    ``reminders_closed`` flag on the approval-version state (set by an edit or a
    past cutoff). Each request still reminds at most once a day (the dated outbox
    key).

    A request submitted while every manager was off is never asked at submit and
    waits on the admin cover list (#370). For those this sweep does a first ask,
    not a reminder, as soon as a manager is available again, without the 30-day
    reminder wait, because the manager was never asked at all.

    Returns:
        How many requests had a reminder (or a first ask) queued this run.
    """
    from app.models import RequestApprovalState
    from app.models.mixins import utcnow
    now = utcnow()
    today = today_toronto()                                       # Toronto day, matching submit routing (#369)
    async with async_session() as session:
        pending = (await session.execute(
            select(RequestRecord).where(RequestRecord.status == "pending")
        )).scalars().all()
        queued = 0
        for record in pending:
            # Only a request a manager can actually decide is reminded (#455): a
            # getting_ready or needs_attention row is skipped here, so the manager
            # is never emailed or texted about something they cannot act on. Those
            # rows surface on the admin stuck list instead.
            if not can_decide(record):
                continue
            # A Postgres request gets an approval-version-state row once it is
            # edited; honour its reminders_closed flag the way reminders.py does.
            state = await session.get(
                RequestApprovalState, (_edit_list_id(record.kind), str(record.item_id))
            )
            if state is not None and state.reminders_closed:
                continue                                          # reminders explicitly closed: skip
            if record.sp_item_id is not None and state is None:
                # Imported, and the old path never emailed its manager (no state
                # row), so the old path never reminded it either: keep it silent.
                continue
            clock = _reminder_clock(record, state)                # continue an imported request's cadence
            if _reminder_cutoff_passed(record, today, clock[1]):
                if state is not None and not state.reminders_closed:
                    state.reminders_closed = True                 # close it so later scans skip without recomputing
                continue
            if not await _manager_ever_asked(session, record):
                # Never asked (all managers were off at submit): ask the first
                # available one now, bypassing the reminder cadence (#370).
                if await _first_ask(session, record, today):
                    queued += 1
                continue
            if not _reminder_due(record, now, clock):
                continue
            if await _queue_reminder(session, record.kind, record.item_id, today):
                queued += 1
        await session.commit()
    if queued:
        _wake_sender()
    return queued


# The SharePoint list id and the version-snapshot fields per kind, so an admin
# edit keeps the approval route's get_current_version in step with the typed one.
def _edit_list_id(kind: str) -> str:
    """The SharePoint list id a kind's approval-version state is keyed under."""
    return {
        "leave": settings.SP_LIST_LEAVE_REQUESTS,
        "overtime": settings.SP_LIST_OVERTIME_REQUESTS,
        "carryover_payout": settings.SP_LIST_CARRYOVER_PAYOUT,
    }[kind]


async def _sync_version_state(session, kind, item_id, version, before, after) -> None:
    """Hold the approval-version state at the typed row's new version after an edit.

    The approval link route gates a click on ``get_current_version`` (the
    ``request_approval_state`` row), while the Postgres decision path gates on the
    typed ``approval_version``. An edit bumps the typed version, so this writes the
    same number to the version-state row in the same transaction, so the route
    shows the old link as outdated and lets the new link through, both agreeing.

    Args:
        session: The open session the caller commits.
        kind: The request kind.
        item_id: The item_id (the number people and links use).
        version: The new approval version to pin.
        before: The pre-edit field values (its material fields are the previous snapshot).
        after: The post-edit field values (its material fields are the new snapshot).
    """
    from app.models import RequestApprovalState
    from app.models.mixins import utcnow
    from app.services.approval_versions import (
        MATERIAL_FIELDS_CARRYOVER_PAYOUT,
        MATERIAL_FIELDS_LEAVE,
        MATERIAL_FIELDS_OVERTIME,
    )
    keys = {
        "leave": MATERIAL_FIELDS_LEAVE,
        "overtime": MATERIAL_FIELDS_OVERTIME,
        "carryover_payout": MATERIAL_FIELDS_CARRYOVER_PAYOUT,
    }[kind]
    list_id = _edit_list_id(kind)
    snapshot = {k: after.get(k) for k in keys}                    # the links now reflect these values
    previous = {k: before.get(k) for k in keys}
    # request_approval_state.last_emailed_at is a naive DateTime column, so it
    # takes naive UTC (as approval_versions.py writes it); an aware value here
    # makes asyncpg reject the whole edit. _naive_utc drops the tzinfo.
    emailed_at = _naive_utc(utcnow())
    state = await session.get(RequestApprovalState, (list_id, str(item_id)))
    if state is None:                                             # a Postgres request has no row until its first edit
        session.add(RequestApprovalState(
            list_id=list_id, item_id=str(item_id), current_version=version,
            current_snapshot=snapshot, previous_snapshot=previous,
            last_emailed_at=emailed_at, reminder_count=0, reminders_closed=False,
        ))
    else:
        state.previous_snapshot = state.current_snapshot
        state.current_snapshot = snapshot
        state.current_version = version
        state.last_emailed_at = emailed_at
        state.reminder_count = 0                                  # a material change restarts the reminder cadence
        state.reminders_closed = False


async def apply_admin_edit(kind, item_id, before, after, reason, *,
                           actor="admin", channel="admin_dashboard") -> dict:
    """Apply an admin's edit to a pending request and ask the manager again (#324).

    In one transaction it writes the edited fields onto the typed row, bumps the
    approval version so the previously emailed links go stale, records each field
    change, appends an ``edited`` event and queues fresh manager approval email and
    text through the outbox carrying the new version's links. No employee receipt
    is sent (it is a re-ask, not a new submission). The outbox sender delivers the
    links, so editing never waits on the email provider.

    Args:
        kind: The request kind ("leave", "overtime", "carryover_payout").
        item_id: The item_id people and links use.
        before: The field values before the edit (SharePoint column names).
        after: The field values after the edit (SharePoint column names), applied
            onto the typed row.
        reason: Why the admin edited it, recorded on each change and the event.
        actor: Who edited, for the change rows and the event.
        channel: Where the edit came from (a request_vocab channel).

    Returns:
        ``{"status": "saved", "new_version", "fields"}`` on success, or
        ``{"error": ...}`` when the request is gone or no longer pending.
    """
    from app.models.mixins import utcnow
    async with async_session() as session:
        record = await _load(session, kind, item_id)
        if record is None:
            return {"error": "This request no longer exists."}
        if record.status != "pending":                           # a decided request cannot be edited
            return {"error": f"Request is not Pending (current status: {record.status.capitalize()})"}

        adapter = TypedRequestList(kind)
        employee = await _resolve_employee(record, adapter._item_shape(record)["fields"])
        managers = await get_all_managers_for_employee(employee) if employee else []
        # Re-ask only the first available manager, so an edit routes past off
        # managers the same way a submission does (#369).
        chosen, skipped = (
            await choose_available_manager(session, managers, today_toronto()) if managers else (None, [])
        )
        _assign_manager(record, chosen)                          # the edit's re-ask follows whoever is asked

        await adapter._resolve_people(record, after)             # keep names in step in production (no-op in tests)
        adapter._apply_fields(record, after)                     # write the edited values onto the typed columns
        new_version = (record.approval_version or 1) + 1
        record.approval_version = new_version                    # fresh links; the old ones go stale
        record.updated_at = utcnow()
        # An edit re-asks the manager, so the request is decidable again under its
        # new version: it stays (or returns to) waiting_on_approval (#455).
        move(record, "waiting_on_approval", reason="admin edit")
        await session.flush()                                    # enforce the CHECK constraints before recording

        for col, new_val in after.items():                       # one change row per edited field, with the reason
            record_change(session, record, col, before.get(col), new_val, actor, channel, reason)
        await _sync_version_state(session, kind, record.item_id, new_version, before, after)

        edited_key = step_key(kind, record.item_id, "edited", new_version)
        event = await append_event(session, record, "edited", channel, actor, {"reason": reason}, edited_key)
        if employee is not None and managers:                    # ask the manager again with the new links, no receipt
            await _enqueue_notifications(
                session, record, employee, [chosen] if chosen else [], event=event, include_receipt=False,
            )
            await _record_reroute(session, record, chosen, skipped, channel, f"edit:v{new_version}")
        log_step(record, "edited", actor=actor, version=new_version)
        await session.commit()
    _wake_sender()                                               # deliver the new manager links without waiting
    return {"status": "saved", "new_version": new_version, "fields": after}
