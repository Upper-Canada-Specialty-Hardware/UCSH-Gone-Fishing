"""Staff change or cancel their own PENDING request (#518, #519).

Two things a staff member can do to a request that is still waiting on a manager:

* Change it (#518, leave only). When a new leave request shares a day with one
  of their pending requests, the request page offers to change that request to
  the new dates and type instead of refusing. The change keeps the same request
  and number: it goes through the same edit path an admin edit uses
  (``request_submission.apply_admin_edit``), so the approval version goes up and
  the links in earlier emails stop working, the manager is asked again with
  fresh links and a line saying what changed, and the person gets a fresh
  receipt. Every email keeps the request's one subject, so it all stays in one
  thread (#520).
* Cancel it (#519, any kind). From History, from the request page after
  sending, or from the link in their receipt email. The request becomes
  "cancelled", nothing moves on any balance (a pending request has moved none),
  the manager and the person are told in the request's thread, and an approve or
  reject from an old link says it was cancelled.

Only pending requests: approved time off is undone by an admin's refund, never
by the person. Both run under the same per-employee lock the approve path uses,
with the row re-read under FOR UPDATE, so a cancel or change and an approval
arriving together cannot both win.
"""

import html
import logging

from app.database import async_session
from app.models.mixins import utcnow
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.app_settings import toronto_today
from app.services.balance import booking_refusal
from app.services.business_days import calculate_business_days
from app.services.cancel_links import decode_cancel_token
from app.services.concurrency import lock_manager
from app.services.employee import get_employee_by_id, map_location_to_province
from app.services.holidays import get_half_friday_season, get_holidays_for_province
from app.services.overlap_detection import _readable_date, _readable_range
from app.services.request_decisions import _wake_sender
from app.services.request_descriptions import compose_leave_title
from app.services.request_intake import PendingClash, RequestFormError, refuse_leave_clashes
from app.services.request_journal import append_event, enqueue_message, log_step, record_change, step_key
from app.services.request_states import can_decide, move
from app.services.request_submission import _load, apply_admin_edit

logger = logging.getLogger(__name__)

# A part-day request's hours become days at this rate, as the submit path does.
HOURS_PER_DAY = 8.0

# The url spelling of a kind (what the request page and links use) -> the stored one.
KIND_FROM_URL = {"leave": "leave", "overtime": "overtime", "carryover-payout": "carryover_payout"}

GONE_MESSAGE = "That request could not be found."


# A pending request still being set up (not yet sent to a manager) cannot be cancelled yet (#455).
NOT_READY_MESSAGE = "This request is still being set up. Try again in a minute."


class OwnRequestError(Exception):
    """A change or cancel that cannot be done; the message is for the person.

    Attributes:
        status_code: The HTTP status the route answers with (400, 404 or 409).
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ----- words -----


def _days_words(days) -> str:
    """A day count in words: "1 day", "0.5 days", "5 days".

    Args:
        days: The number of days, or None.

    Returns:
        The phrase, or "" when there is no count.
    """
    if days is None:
        return ""
    value = float(days)
    shown = f"{value:g}"                                       # 5.0 -> "5", 0.5 -> "0.5"
    return f"{shown} day" if value == 1 else f"{shown} days"


def summary_of(kind: str, fields: dict) -> str:
    """One request in a short line, e.g. "Vacation, Nov 2, 2026 to Nov 6, 2026 (5 days)".

    Args:
        kind: The stored kind.
        fields: The request in the SharePoint field shape.

    Returns:
        The line the cancel page and the change and cancel emails show.
    """
    if kind == "leave":
        start, end = fields.get("StartDate") or "", fields.get("EndDate") or fields.get("StartDate") or ""
        when = _readable_range(start[:10], end[:10]) if start else ""
        days = _days_words(fields.get("Days"))
        return f"{fields.get('LeaveType') or 'Leave'}, {when}" + (f" ({days})" if days else "")
    if kind == "overtime":
        when = _readable_date((fields.get("StartDate") or "")[:10])
        hours = fields.get("Hours")
        return f"Time make-up, {when}" + (f" ({float(hours):g} hours)" if hours is not None else "")
    option = fields.get("TypeofRequest") or "Carry-over or payout"   # "Carry Over" / "Payout"
    days = _days_words(fields.get("Days"))
    return f"{option}, {days}" if days else option


def _note_html(text: str) -> str:
    """A bold line put at the top of a change or cancel email.

    Args:
        text: The plain sentence(s); escaped here.

    Returns:
        One styled HTML paragraph.
    """
    return (
        '<p style="font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.5;'
        f'color:#2f5d50;font-weight:bold;margin:0 0 12px;">{html.escape(text)}</p>'
    )


def _not_pending_message(record: RequestRecord) -> str:
    """Why a decided request cannot be changed or cancelled by its owner.

    Args:
        record: The request, no longer pending.

    Returns:
        A sentence saying what it became and what to do instead.
    """
    number = record.item_id
    if record.status == "approved":
        return (f"Request #{number} is already approved, so it cannot be changed or cancelled here. "
                "To undo approved time off, ask an admin for a refund.")
    if record.status == "cancelled":
        return f"Request #{number} was already cancelled."
    return f"Request #{number} was already {record.status}, so there is nothing to change or cancel."


# ----- change (#518) -----


async def _leave_days(form, emp_fields: dict) -> float:
    """The days a leave form counts, the way the submit path works them out.

    A part day is its hours over 8. A full-day leave is the business days in its
    range: weekends and the person's provincial holidays skipped, a half Friday
    in season counted as half.

    Args:
        form: The checked ``LeaveForm``.
        emp_fields: The person's staff fields (Location picks the holidays).

    Returns:
        The number of days.
    """
    if form.partial_hours is not None:                          # a part day: hours / 8
        return float(form.partial_hours) / HOURS_PER_DAY
    try:
        province = map_location_to_province(emp_fields.get("Location", ""))
    except ValueError:
        province = None                                         # an unmapped location counts no holidays
    holidays = await get_holidays_for_province(province) if province else []
    return calculate_business_days(form.start_date, form.end_date, holidays, get_half_friday_season(holidays))


def _same(a, b) -> bool:
    """Whether two field values are the same, numbers compared as numbers."""
    try:
        return float(a) == float(b)                             # 5 and 5.0 are the same day count
    except (TypeError, ValueError):
        return (a or "") == (b or "")


async def change_own_leave(item_id, employee: dict, form) -> dict:
    """Change a staff member's own pending leave to what they just filled in (#518).

    Args:
        item_id: The pending leave request to change.
        employee: The signed-in person's staff record ({"id", "fields"}).
        form: The checked ``LeaveForm`` with the new values.

    Returns:
        ``{"status": "changed", "item_id"}``.

    Raises:
        OwnRequestError: Not theirs or gone (404); no longer pending, or the new
            days clash with another request (409); the booking limit, or nothing
            changed (400).
    """
    emp_fields = employee.get("fields", {})
    staff_id = str(employee.get("id") or "")
    name = (emp_fields.get("Title") or "").strip()
    email = (emp_fields.get("EmailAddress") or "").strip().lower()
    async with async_session() as session:
        record = await _load(session, "leave", item_id)
        if record is None or record.employee_id != staff_id:
            raise OwnRequestError(GONE_MESSAGE, 404)            # someone else's reads as gone
        if record.status != "pending":
            raise OwnRequestError(_not_pending_message(record), 409)
        current = TypedRequestList("leave")._item_shape(record)["fields"]   # what it says today

    # The same refusals a new request gets, with this request left out of the clash check.
    refusal = booking_refusal(emp_fields, form.leave_type, form.end_date, toronto_today())
    if refusal:
        raise OwnRequestError(refusal, 400)
    try:
        await refuse_leave_clashes(
            form.start_date.isoformat(), form.end_date.isoformat(), email, staff_id, exclude_item_id=item_id,
        )
    except PendingClash as e:                                  # it would now overlap a second pending request
        other = e.clashes[0]["item_id"]
        raise OwnRequestError(
            f"Those days also overlap your pending request #{other}. Cancel that one first, then change this one.",
            409,
        ) from e
    except RequestFormError as e:                              # approved leave on one of the new days
        raise OwnRequestError(str(e), 409) from e

    days = await _leave_days(form, emp_fields)
    keys = ("LeaveType", "StartDate", "EndDate", "Days", "Title")
    before = {k: current.get(k) for k in keys}
    after = {
        "LeaveType": form.leave_type,
        "StartDate": form.start_date.isoformat(),
        "EndDate": form.end_date.isoformat(),
        "Days": days,
        "Title": compose_leave_title(name, form.notes),        # "<name> - <notes>", as at submit
    }
    if all(_same(before[k], after[k]) for k in keys):
        raise OwnRequestError(f"Request #{item_id} already says that, so nothing was changed.", 400)

    was, now = summary_of("leave", before), summary_of("leave", after)
    manager_note = _note_html(
        f"{name or 'They'} changed this request. It was: {was}. It is now: {now}. "
        "The buttons in earlier emails no longer work; use the ones below."
    )
    receipt_note = _note_html(f"You changed this request. It was: {was}. It is now: {now}.")
    result = await apply_admin_edit(
        "leave", item_id, before, after, "Changed by the employee on the request page",
        actor=name or "employee", channel="request_page", employee_id=staff_id,
        manager_note=manager_note, receipt_note=receipt_note,
    )
    if "error" in result:                                      # decided or gone in the meantime
        raise OwnRequestError(result["error"], 409)
    logger.info("Leave #%s changed by its owner (staff %s)", item_id, staff_id)
    return {"status": "changed", "item_id": str(item_id)}


# ----- cancel (#519) -----


async def cancel_own_request(kind: str, item_id, staff_id, *, channel: str) -> dict:
    """Cancel a staff member's own pending request and tell the manager (#519).

    Args:
        kind: The stored kind ("leave", "overtime", "carryover_payout").
        item_id: The request number.
        staff_id: The staff id it must belong to.
        channel: Where the cancel came from ("request_page", "employee_dashboard"
            or "email_link").

    Returns:
        ``{"status": "cancelled", "item_id", "already"}``; ``already`` is True
        when it had been cancelled before (a second click changes nothing).

    Raises:
        OwnRequestError: Not theirs or gone (404); decided already (409).
    """
    async with async_session() as session:
        record = await _load(session, kind, item_id)
        if record is None or record.employee_id != str(staff_id):
            raise OwnRequestError(GONE_MESSAGE, 404)           # someone else's reads as gone
        async with lock_manager.lock(record.employee_id):      # the approve path holds the same lock
            await session.refresh(record, with_for_update=True)   # see a decision committed a moment ago
            if record.status == "cancelled":
                return {"status": "cancelled", "item_id": str(record.item_id), "already": True}
            if record.status != "pending":
                raise OwnRequestError(_not_pending_message(record), 409)
            if not can_decide(record):                         # still being set up: it has not reached a manager (#455)
                raise OwnRequestError(NOT_READY_MESSAGE, 409)
            await _cancel_locked(session, record, channel)
            await session.commit()
    _wake_sender()                                             # deliver the notices without waiting
    return {"status": "cancelled", "item_id": str(item_id), "already": False}


async def _cancel_locked(session, record: RequestRecord, channel: str) -> None:
    """Mark the request cancelled, record it, and queue the two notices (no commit).

    Args:
        session: The open session.
        record: The pending request, locked.
        channel: Where the cancel came from.
    """
    name = record.employee_name or "The employee"
    record.status = "cancelled"                                # terminal: no manager can act on it now
    move(record, "cancelled", reason="cancelled by the employee")   # the lifecycle state follows (#455)
    record.updated_at = utcnow()
    record_change(session, record, "Status", "Pending", "Cancelled", name, channel, "Cancelled by the employee")
    key = step_key(record.kind, record.item_id, "cancelled", record.approval_version)
    event = await append_event(session, record, "cancelled", channel, name, {"by": "employee"}, key)

    summary = summary_of(record.kind, TypedRequestList(record.kind)._item_shape(record)["fields"])
    # The manager who was asked: told in the request's thread, by email and, as
    # the ask went by text too, by text.
    manager = await get_employee_by_id(record.manager_id) if record.manager_id else None
    if manager:
        mf = manager.get("fields", {})
        mgr_email = (mf.get("EmailAddress") or "").strip()
        if mgr_email:
            body = _note_html(f"{name} cancelled this request, so it no longer needs your decision.") + (
                '<p style="font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.5;'
                f'color:#1f1a14;margin:0 0 12px;">Request #{record.item_id}: {html.escape(summary)}.<br>'
                "The approve and reject buttons in the earlier emails no longer work.</p>"
            )
            await enqueue_message(session, record, event, "cancel_notice", "email", mgr_email, None, body,
                                  f"{key}:email:{mgr_email}")
        cell = (mf.get("CellNumber") or "").strip()
        if cell:
            text = (f"Request #{record.item_id} for {name} ({summary}) was cancelled by {name}. "
                    "No reply needed.")
            await enqueue_message(session, record, event, "cancel_notice", "sms", cell, None, text,
                                  f"{key}:sms:{cell}")
    if record.employee_email:                                   # the person's own confirmation
        body = _note_html("You cancelled this request.") + (
            '<p style="font-family:\'Segoe UI\',Arial,sans-serif;font-size:14px;line-height:1.5;'
            f'color:#1f1a14;margin:0 0 12px;">Request #{record.item_id}: {html.escape(summary)}.<br>'
            "Nothing else is needed. If you still need time off, send a new request.</p>"
        )
        await enqueue_message(session, record, event, "cancel_notice", "email", record.employee_email, None, body,
                              f"{key}:email:{record.employee_email}")
    log_step(record, "cancelled", channel=channel)


# ----- the cancel link in the receipt (#519) -----


async def cancel_page_info(token: str) -> dict:
    """What the public cancel page shows for a receipt's cancel link.

    Args:
        token: The signed token from the link.

    Returns:
        ``{"state", "request_number", "summary"}``. ``state`` is "open" while it
        can be cancelled, else what it became ("cancelled", "approved",
        "rejected", "refunded"), or "invalid" for a forged or gone link.
    """
    decoded = decode_cancel_token(token)
    if decoded is None:
        return {"state": "invalid", "request_number": "", "summary": ""}
    kind, item_id, staff_id = decoded
    async with async_session() as session:
        record = await _load(session, kind, item_id)
        if record is None or record.employee_id != staff_id:
            return {"state": "invalid", "request_number": "", "summary": ""}
        fields = TypedRequestList(kind)._item_shape(record)["fields"]
        state = "open" if record.status == "pending" else record.status
        return {"state": state, "request_number": str(record.item_id), "summary": summary_of(kind, fields)}


async def cancel_from_link(token: str) -> dict:
    """Cancel the request a receipt's link names, then say what the page should show.

    Args:
        token: The signed token from the link, sent by the page's confirm button.

    Returns:
        The ``cancel_page_info`` shape, with ``state`` "done" when this click
        cancelled it.
    """
    decoded = decode_cancel_token(token)
    if decoded is None:
        return {"state": "invalid", "request_number": "", "summary": ""}
    kind, item_id, staff_id = decoded
    try:
        result = await cancel_own_request(kind, item_id, staff_id, channel="email_link")
    except OwnRequestError:
        return await cancel_page_info(token)                   # decided or gone: say what it is now
    info = await cancel_page_info(token)
    return {**info, "state": "done"} if not result["already"] else info
