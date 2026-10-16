"""One decision path for every channel, writing the whole outcome in one go (#324).

A manager or admin can decide a request from five places: the email link, an SMS
reply, and the employee, manager and admin dashboards (plus a manager logging a
sick day on someone's behalf). They all converge here. ``approve``, ``reject``
and ``refund`` each do the complete decision in ONE session and ONE transaction,
so the status change, the balance pots, the ledger rows, the timeline events and
the queued result messages all land together or not at all.

This fixes a production bug: the old path sent the "Approved" email before the
balance was written, so an email-provider timeout could leave a request approved
with its balance never updated, or updated without the employee ever told. Here
the balance is written and the result message is only queued; a separate sender
delivers it (app/services/outbox_sender.py, app/tasks/outbox_worker.py), so
approving never waits on email and nothing is half done.

Idempotency is layered: the per-employee asyncio lock serialises one person's
decisions in this process, the status is re-checked under that lock, and every
write carries a ``step_key`` that is unique in its table, so the same decision
arriving from two channels at once still lands exactly once.

This path runs only once requests AND staff both live in Postgres (see
``decisions_active``); until then every channel keeps running the old SharePoint
path unchanged.
"""
import logging
from datetime import date

from sqlalchemy import select

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.request_record import RequestRecord
from app.repositories.request_store import requests_in_postgres
from app.repositories.staff_balances import POT_COLUMNS, get_staff_balances, staff_in_postgres
from app.services.balance import (
    is_next_year_request,
    simulate_carryover_payout_impact,
    simulate_leave_impact,
    simulate_overtime_impact,
)
from app.services.concurrency import lock_manager
from app.services.employment import (
    HOURLY_TYPES,
    NO_COST_LEAVE,
    leave_costs_balance,
    overtime_costs_balance,
)
from app.services.overlap_detection import approval_refusal, find_leave_conflict, find_overtime_conflict
from app.services.request_journal import append_event, enqueue_message, log_step, post_ledger, step_key
from app.templates_render import render_request_result

logger = logging.getLogger(__name__)

# Pot -> balance column the other way, to read a simulate_* result back as pots.
SP_FIELD_TO_POT = {column: pot for pot, column in POT_COLUMNS.items()}
# A carry-over/payout request's stored option -> the word the simulation wants.
REQUEST_OPTION_WIRE = {"carry_over": "Carry Over", "payout": "Payout"}
# The request kind in words, for subjects and sentences.
KIND_LABEL = {"leave": "Leave", "overtime": "Overtime", "carryover_payout": "Carry Over / Payout"}
# The balance pot in words, for the result email and text.
POT_LABEL = {
    "sick": "Sick/Personal",
    "make_up": "Overtime (Make-Up)",
    "carry_over": "Carry Over",
    "vacation": "Vacation",
    "payout": "Payout",
    "vacation_taken": "Vacation taken this year",
}

# The message a stale link or an already-decided request shows the manager.
STALE_MESSAGE = "This request has already been updated; your link is out of date. Please use the latest email."
ALREADY_DECIDED_MESSAGE = "Already processed"
NOT_APPROVED_MESSAGE = "Only approved requests can be refunded"
GONE_MESSAGE = "This request no longer exists."
CARRYOVER_NEGATIVE_MESSAGE = "This would take vacation below zero, so it was not approved."


async def decisions_active() -> bool:
    """Whether the Postgres decision path is the one that runs now.

    It runs only once the request lists AND the staff balances both live in
    Postgres, because the balance step writes pots through ``StaffBalances`` and
    only the Postgres staff backend can do that inside this transaction. Until
    staff moves too, every channel keeps the old SharePoint path (the contract's
    "staff and requests switch to Postgres together").

    Returns:
        True when approve/reject/refund should route through this module.
    """
    return await requests_in_postgres() and await staff_in_postgres()


def _wake_sender() -> None:
    """Nudge the outbox worker so a just-queued result message goes out at once.

    Best-effort: the worker also polls, so a missing worker (tests) is harmless.
    """
    try:
        from app.tasks.outbox_worker import kick_outbox
        kick_outbox()
    except Exception:                                              # never let a nudge break a committed decision
        pass


async def _load(session, kind: str, item_id) -> RequestRecord | None:
    """The request row for one (kind, item_id), or None when there is none.

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


def _is_hourly(request: RequestRecord) -> bool:
    """Whether the request was decided under the hourly rules.

    Uses the employment type snapshot stored on the request (how the person was
    paid when the request applied); a null legacy value counts as salaried.

    Args:
        request: The request row.

    Returns:
        True for Hourly and Hourly BC.
    """
    return (request.employment_type or "") in HOURLY_TYPES


def _sp_fields(before_pots: dict[str, float], request: RequestRecord) -> dict:
    """Build a Staff-Directory-shaped balance dict the simulate_* functions read.

    Args:
        before_pots: The pots as read from StaffBalances.
        request: The request, for its employment type (simulate branches on it).

    Returns:
        ``{balance column: value, "SalaryHourly": type}``.
    """
    sp = {column: before_pots[pot] for pot, column in POT_COLUMNS.items()}
    sp["SalaryHourly"] = request.employment_type or "Salary"       # null legacy -> salaried rules
    return sp


def _moved_pots(before_pots: dict[str, float], after_sp: dict) -> dict[str, float]:
    """The pots a simulation changed, as ``{pot: new_value}``.

    Args:
        before_pots: The pots before the decision.
        after_sp: A simulate_* result in balance-column names.

    Returns:
        Only the pots whose value differs from before.
    """
    moved: dict[str, float] = {}
    for sp_field, value in after_sp.items():
        pot = SP_FIELD_TO_POT.get(sp_field)                        # ignore SalaryHourly and any extra key
        if pot is not None and float(value) != before_pots[pot]:
            moved[pot] = float(value)
    return moved


def _leave_year(request: RequestRecord) -> int:
    """The calendar year a leave's vacation counts toward (its start date's year).

    Args:
        request: The leave request.

    Returns:
        The start date's year, or this year when there is no start date.
    """
    return request.start_date.year if request.start_date else date.today().year


def _plan_balance(request: RequestRecord, before_pots: dict[str, float]) -> tuple[str, dict[str, float], str]:
    """Work out, purely, what approving this request does to the balance.

    Uses the balance engine's pure ``simulate_*`` functions (the single source
    of truth the dashboards use), so this can never drift from the real
    deduction. It writes nothing.

    Args:
        request: The request being approved.
        before_pots: The employee's pots before the decision.

    Returns:
        ``(outcome, moved_pots, note)`` where outcome is ``"balance_applied"``,
        ``"counted"`` (hourly vacation), ``"no_balance_used"`` or
        ``"refused_negative"`` (carry-over/payout that would go below zero);
        moved_pots is ``{pot: new_value}`` for a salaried/hourly-sick move; note
        is the plain-words reason when nothing was used.
    """
    sp = _sp_fields(before_pots, request)
    if request.kind == "leave":
        leave_type = request.leave_type or ""
        days = float(request.days or 0)
        if leave_type in NO_COST_LEAVE:                            # bereavement, jury duty: never a balance
            return "no_balance_used", {}, f"{leave_type} does not use a balance."
        if not leave_costs_balance(sp, leave_type):                # hourly vacation: counted, not spent
            return "counted", {}, ""
        after = simulate_leave_impact(sp, leave_type, days, is_next_year=_is_next_year(request))
        if after is None:                                          # defensive: a no-impact type slipped through
            return "no_balance_used", {}, f"{leave_type} does not use a balance."
        return "balance_applied", _moved_pots(before_pots, after), ""
    if request.kind == "overtime":
        if not overtime_costs_balance(sp):                         # hourly overtime banks nothing
            return "no_balance_used", {}, "Overtime is paid for hourly staff, so no balance was banked."
        after = simulate_overtime_impact(sp, float(request.hours or 0))
        return "balance_applied", _moved_pots(before_pots, after), ""
    # carry-over / payout
    req_type = REQUEST_OPTION_WIRE.get(request.request_option or "", "Carry Over")
    after = simulate_carryover_payout_impact(sp, float(request.days or 0), req_type)
    if after is None:                                              # vacation would go below zero: refuse
        return "refused_negative", {}, ""
    return "balance_applied", _moved_pots(before_pots, after), ""


def _is_next_year(request: RequestRecord) -> bool:
    """Whether a leave falls in next year (its cascade differs).

    Args:
        request: The leave request.

    Returns:
        True when the start or end date is next year.
    """
    start = request.start_date
    end = request.end_date or start
    if start is None:
        return False
    return is_next_year_request(start, end)


async def _vacation_taken_for_year(session, employee_id: str, year: int) -> float:
    """Vacation days an hourly person has already taken in a year, from the ledger.

    The hourly vacation counter is not stored; it is the sum of the
    ``vacation_taken`` ledger rows whose request's start date is in the year, so
    it rises as requests are approved and falls when one is refunded.

    Args:
        session: An open session.
        employee_id: Whose counter to read.
        year: The calendar year to total.

    Returns:
        The days taken, before this decision.
    """
    rows = (await session.execute(
        select(BalanceLedger.change, RequestRecord.start_date)
        .join(RequestRecord, BalanceLedger.request_id == RequestRecord.id)
        .where(BalanceLedger.employee_id == str(employee_id), BalanceLedger.pot == "vacation_taken")
    )).all()
    return sum(float(change) for change, start_date in rows if start_date and start_date.year == year)


async def _key_committed(session, model, key: str) -> bool:
    """Whether an idempotency key is already present (a prior attempt committed it).

    Args:
        session: An open session.
        model: The table to look in (BalanceLedger here).
        key: The idempotency key.

    Returns:
        True when a row with that key already exists.
    """
    return (await session.execute(
        select(model.id).where(model.idempotency_key == key)
    )).first() is not None


# The overlap finders match rows to a person by lookup id or email. The rows
# handed to them here are already filtered to the one employee by staff id, so
# each carries this same marker and the finder's own person match always passes.
_SAME_PERSON = "same-staff-id"


def _overlap_row(row: RequestRecord) -> dict:
    """Shape an approved request the way the overlap finders read rows.

    Args:
        row: An approved request of the same employee and kind.

    Returns:
        ``{"id", "fields"}`` with the status, dates, days and the person marker.
    """
    start = row.start_date.isoformat() if row.start_date else None
    end = row.end_date.isoformat() if row.end_date else start      # a one-day leave may carry no end date
    return {
        "id": str(row.item_id),                                    # the number a manager sees
        "fields": {
            "Status": "Approved",                                  # only approved rows are passed in
            "StartDate": start,
            "EndDate": end,
            "Days": row.days,                                      # part-days may share a date
            "SubmitterEmail": _SAME_PERSON,                        # already matched by staff id
        },
    }


async def _approval_conflict(session, request: RequestRecord) -> str | None:
    """The refusal when approving would clash with an already-approved request.

    Mirrors the SharePoint approval's check: a leave may not overlap an approved
    leave (part-days may share a date up to a full day), and an overtime entry
    may not share its date with an approved one. The approved rows are read in
    this transaction, after the employee's balance lock, so two clashing
    requests approved at the same moment cannot both pass.

    Args:
        session: The open decision session.
        request: The pending request being approved.

    Returns:
        The plain-words refusal, or None when the dates are free, the kind has
        no clash rule (carry-over/payout), or the employee is unknown.
    """
    if request.kind not in ("leave", "overtime") or request.start_date is None:
        return None                                                # nothing to compare
    if not request.employee_id:
        # Same choice as the SharePoint path: let it through, but say so.
        logger.warning("request %s:%s clash check skipped: no employee id", request.kind, request.item_id)
        return None
    rows = (await session.execute(
        select(RequestRecord).where(
            RequestRecord.kind == request.kind,
            RequestRecord.employee_id == request.employee_id,      # the same person, by staff id
            RequestRecord.status == "approved",                    # only approved time blocks
            RequestRecord.id != request.id,                        # never itself
        )
    )).scalars().all()
    items = [_overlap_row(row) for row in rows]
    start = request.start_date.isoformat()
    if request.kind == "leave":
        end = request.end_date.isoformat() if request.end_date else start
        conflict = find_leave_conflict(items, None, start, end,
                                       days=float(request.days or 0), submitter_email=_SAME_PERSON)
    else:
        conflict = find_overtime_conflict(items, None, start, submitter_email=_SAME_PERSON)
    return approval_refusal(conflict, request.kind) if conflict else None


async def _refuse(session, request, channel, actor, version, why, reason) -> None:
    """Record that an attempted decision was turned down, and log it.

    Args:
        session: The open session (committed by the caller).
        request: The request the attempt was on.
        channel: Where the attempt came from.
        actor: Who tried.
        version: The link/approval version the refusal is keyed on.
        why: A short tag making the key unique per reason (``"stale"`` ...).
        reason: The plain-words reason, stored on the event.
    """
    key = step_key(request.kind, request.item_id, "refused", version, suffix=f"{channel}:{why}")
    await append_event(session, request, "refused", channel, actor, {"reason": reason}, key)
    log_step(request, "refused", channel=channel, actor=actor, why=why)


def _result_context(request: RequestRecord, outcome_word: str, rows: list[dict], note: str) -> tuple[str, str, str]:
    """Build the result email subject, HTML body and SMS text from the step.

    Args:
        request: The decided request (for the name, kind and number).
        outcome_word: "Approved", "Rejected" or "Refunded".
        rows: Balance lines ``[{"label", "value"}]`` for the email table.
        note: A closing sentence, or "".

    Returns:
        ``(subject, html, sms_text)``.
    """
    name = request.employee_name or "there"
    label = KIND_LABEL.get(request.kind, "Request")
    heading = f"{label} Request: {outcome_word}"
    decided_by = f" by {request.manager_name}" if request.manager_name else ""
    verb = {"Approved": "has been approved", "Rejected": "was not approved", "Refunded": "has been refunded"}[outcome_word]
    intro = f"Hi {name}, your {label.lower()} request #{request.item_id} {verb}{decided_by}."
    subject = f"{request.employee_name or 'Staff'} - {label} Request: {outcome_word}"
    html = render_request_result(heading, intro, rows, note)
    # The text says the same in one plain line, with each balance line appended.
    text_lines = [f"{label} request #{request.item_id} {verb}{decided_by}."]
    text_lines += [f"{row['label']}: {row['value']}" for row in rows]
    if note:
        text_lines.append(note)
    return subject, html, " ".join(text_lines)


async def _enqueue_result(session, request, event, version, subject, html, sms_text) -> None:
    """Queue the result email (and text when a phone is known) to the employee.

    The messages go to the outbox in this same transaction, so a result message
    exists only once its ledger rows or its outcome event exist. A separate
    sender delivers them, so approving never waits on the provider.

    Args:
        session: The open session (committed by the caller).
        request: The decided request.
        event: The outcome event the messages follow.
        version: The approval version, for the message keys.
        subject: The email subject.
        html: The email body.
        sms_text: The text message body.
    """
    email = request.employee_email
    if email:                                                      # the address is on the request row
        key = f"{step_key(request.kind, request.item_id, 'result_sent', version)}:email:{email}"
        await enqueue_message(session, request, event, "result", "email", email, subject, html, key)
    # The employee's phone is not on the request; look it up best-effort, and
    # only when SharePoint is reachable (never in tests). With no phone, the
    # email alone goes out.
    phone = await _employee_phone(request.employee_id)
    if phone:
        key = f"{step_key(request.kind, request.item_id, 'result_sent', version)}:sms:{phone}"
        await enqueue_message(session, request, event, "result", "sms", phone, None, sms_text, key)


async def _employee_phone(employee_id: str) -> str | None:
    """The employee's cell number, best-effort, for the result text.

    When staff lives in Postgres the number comes from the staff store, so the
    result text works without any SharePoint connection. Otherwise it keeps the
    SharePoint path, read only when SharePoint is connected (``sp_client.site_id``
    set), which is true in production and false in every SharePoint-mode test, so
    those tests make no call and get no text.

    Args:
        employee_id: Whose number to read.

    Returns:
        The number, or None when it cannot be found.
    """
    from app.services.employee import get_employee_by_id
    if await staff_in_postgres():                                 # staff store serves this without SharePoint
        employee = await get_employee_by_id(employee_id)
    else:
        from app.graph.sharepoint import sp_client
        if getattr(sp_client, "site_id", None) is None:          # not reachable (SharePoint-mode tests): no text
            return None
        employee = await get_employee_by_id(employee_id)
    cell = (employee or {}).get("fields", {}).get("CellNumber") if employee else None
    return (str(cell).strip() or None) if cell else None


async def record_link_opened(kind, item_id, version, channel, actor=None) -> None:
    """Note that a manager opened an approval link, at most once per version per day.

    This never changes a decided request's stage (the stage rule in
    request_journal keeps "Approved" showing), so a late open is only a timeline
    line, not a step backward.

    Args:
        kind: The request kind.
        item_id: The item_id in the link.
        version: The approval version the link carried.
        channel: Where the open came from (``email_link`` or ``sms``).
        actor: Who opened it, when known.
    """
    async with async_session() as session:
        request = await _load(session, kind, item_id)
        if request is None:                                        # gone: nothing to record
            return
        key = step_key(kind, request.item_id, "link_opened", version, suffix=date.today().isoformat())
        await append_event(session, request, "link_opened", channel, actor, {}, key)
        log_step(request, "link_opened", channel=channel, version=version)
        await session.commit()


async def approve(kind, item_id, actor, channel, link_version=None, reason=None) -> dict:
    """Approve one request: move the balance, record it, queue the result.

    Args:
        kind: The request kind ("leave", "overtime", "carryover_payout").
        item_id: The item_id the link or reply named.
        actor: Who approved (a name or id), stored on the decision and events.
        channel: Where it came from (a request_vocab channel).
        link_version: The approval version the link carried, or None (dashboards
            and SMS do not carry one); a mismatch is a stale link.
        reason: Unused for approve; kept so every path has one signature.

    Returns:
        ``{"status": "approved", ...}`` on success, or ``{"error": ...}`` when
        nothing was written (gone, stale, already decided, a clash with an
        approved request, or a carry-over that would go below zero).
    """
    async with async_session() as session:
        request = await _load(session, kind, item_id)
        if request is None:
            return {"error": GONE_MESSAGE}
        version = request.approval_version
        if link_version is not None and int(link_version) != version:   # the link is out of date
            await _refuse(session, request, channel, actor, link_version, "stale", STALE_MESSAGE)
            await session.commit()
            return {"error": STALE_MESSAGE, "refused": "stale"}
        if request.status != "pending":                            # already decided by another channel
            await _refuse(session, request, channel, actor, version, "decided", ALREADY_DECIDED_MESSAGE)
            await session.commit()
            return {"error": ALREADY_DECIDED_MESSAGE, "refused": "decided"}

        async with lock_manager.lock(request.employee_id):         # one employee's decisions, one at a time
            # Lock the request row (Postgres FOR UPDATE; a no-op on SQLite) and re-read it:
            # a decision another channel or another server just committed is seen here.
            await session.refresh(request, with_for_update=True)
            if request.status != "pending":
                await _refuse(session, request, channel, actor, version, "decided", ALREADY_DECIDED_MESSAGE)
                await session.commit()
                return {"error": ALREADY_DECIDED_MESSAGE, "refused": "decided"}

            balances = await get_staff_balances()                  # SharePoint or Postgres, per the setting
            before_pots = await balances.lock_pots(session, request.employee_id)
            # Checked after the balance lock so a clashing approval committed a
            # moment ago (on any server) is seen; nothing is written on a clash.
            clash = await _approval_conflict(session, request)
            if clash:                                              # the manager rejects or cancels the other one
                await _refuse(session, request, channel, actor, version, "conflict", clash)
                await session.commit()
                return {"error": clash, "refused": "conflict"}
            outcome, moved, note = _plan_balance(request, before_pots)
            if outcome == "refused_negative":                      # carry-over/payout past zero: refuse, write nothing
                await _refuse(session, request, channel, actor, version, "negative", CARRYOVER_NEGATIVE_MESSAGE)
                await session.commit()
                return {"error": CARRYOVER_NEGATIVE_MESSAGE, "refused": "negative"}

            decision_reason = f"approved {kind} #{request.item_id}"
            rows: list[dict] = []
            if outcome == "balance_applied" and moved:
                # Ledger-key-before-SharePoint-write: if the ledger already holds
                # this step (a prior attempt committed it), the balance is already
                # moved, so do not write it to SharePoint a second time.
                first_key = step_key(kind, request.item_id, f"ledger:{next(iter(moved))}", version)
                if not await _key_committed(session, BalanceLedger, first_key):
                    await balances.write_pots(session, request.employee_id, moved)
                for pot, after_value in moved.items():
                    key = step_key(kind, request.item_id, f"ledger:{pot}", version)
                    await post_ledger(session, request, request.employee_id, pot,
                                      before_pots[pot], after_value, decision_reason, actor, channel, key)
                    rows.append({"label": POT_LABEL[pot], "value": f"{before_pots[pot]:g} -> {after_value:g} days"})
                request.balance_state = "applied"
                event_type = "balance_applied"
            elif outcome == "counted":                             # hourly vacation: a counter, no pot moves
                year = _leave_year(request)
                before_count = await _vacation_taken_for_year(session, request.employee_id, year)
                days = float(request.days or 0)
                key = step_key(kind, request.item_id, "ledger:vacation_taken", version)
                await post_ledger(session, request, request.employee_id, "vacation_taken",
                                  before_count, before_count + days, decision_reason, actor, channel, key)
                rows.append({"label": POT_LABEL["vacation_taken"],
                             "value": f"{before_count:g} -> {before_count + days:g} days"})
                request.balance_state = "counted"
                event_type = "counted"
            else:                                                  # no_balance_used (no-cost leave, hourly overtime)
                request.balance_state = "no_balance"
                event_type = "no_balance_used"

            _mark_decided(request, "approved", actor, channel)
            approved_key = step_key(kind, request.item_id, "approved", version)
            await append_event(session, request, "approved", channel, actor, {}, approved_key)
            outcome_key = step_key(kind, request.item_id, event_type, version)
            event = await append_event(session, request, event_type, channel, actor, {"note": note}, outcome_key)
            subject, html, sms_text = _result_context(request, "Approved", rows, note)
            await _enqueue_result(session, request, event, version, subject, html, sms_text)
            log_step(request, "approved", channel=channel, actor=actor, outcome=event_type)
            await session.commit()
        _wake_sender()                                             # send the result without waiting for the poll
        return {"status": "approved", "balance_state": request.balance_state}


async def reject(kind, item_id, actor, channel, link_version=None, reason=None) -> dict:
    """Reject one request: record it and queue the rejection email. No balance moves.

    Args:
        kind: The request kind.
        item_id: The item_id.
        actor: Who rejected.
        channel: Where it came from.
        link_version: The link's version, or None; a mismatch is a stale link.
        reason: The rejection reason shown to the employee, or None.

    Returns:
        ``{"status": "rejected"}`` or ``{"error": ...}``.
    """
    async with async_session() as session:
        request = await _load(session, kind, item_id)
        if request is None:
            return {"error": GONE_MESSAGE}
        version = request.approval_version
        if link_version is not None and int(link_version) != version:
            await _refuse(session, request, channel, actor, link_version, "stale", STALE_MESSAGE)
            await session.commit()
            return {"error": STALE_MESSAGE, "refused": "stale"}
        if request.status != "pending":
            await _refuse(session, request, channel, actor, version, "decided", ALREADY_DECIDED_MESSAGE)
            await session.commit()
            return {"error": ALREADY_DECIDED_MESSAGE, "refused": "decided"}

        async with lock_manager.lock(request.employee_id):
            await session.refresh(request, with_for_update=True)  # lock the row, see a decision just committed
            if request.status != "pending":
                await _refuse(session, request, channel, actor, version, "decided", ALREADY_DECIDED_MESSAGE)
                await session.commit()
                return {"error": ALREADY_DECIDED_MESSAGE, "refused": "decided"}

            _mark_decided(request, "rejected", actor, channel)
            rejected_key = step_key(kind, request.item_id, "rejected", version)
            event = await append_event(session, request, "rejected", channel, actor, {"reason": reason}, rejected_key)
            note = reason or "The request was not approved."
            subject, html, sms_text = _result_context(request, "Rejected", [], note)
            await _enqueue_result(session, request, event, version, subject, html, sms_text)
            log_step(request, "rejected", channel=channel, actor=actor)
            await session.commit()
        _wake_sender()                                             # send the rejection without waiting for the poll
        return {"status": "rejected"}


async def refund(kind, item_id, actor, channel, link_version=None, reason=None) -> dict:
    """Refund an approved request: reverse exactly what its ledger rows moved.

    For every pot the approval moved, a new ledger row with the opposite change
    is written and the pot is put back; a hourly vacation counter row is reversed
    the same way. The request becomes ``refunded`` and a ``balance_reversed``
    event is recorded, then the refund notice is queued.

    Args:
        kind: The request kind.
        item_id: The item_id.
        actor: Who refunded (an admin).
        channel: Where it came from.
        link_version: Unused for refunds (admin only); kept for one signature.
        reason: Unused; kept for one signature.

    Returns:
        ``{"status": "refunded", ...}`` or ``{"error": ...}``.
    """
    async with async_session() as session:
        request = await _load(session, kind, item_id)
        if request is None:
            return {"error": GONE_MESSAGE}
        if request.status != "approved":                           # only an approved request can be refunded
            return {"error": NOT_APPROVED_MESSAGE}
        version = request.approval_version

        async with lock_manager.lock(request.employee_id):
            await session.refresh(request, with_for_update=True)  # lock the row, see a decision just committed
            if request.status != "approved":                       # another refund got here first
                return {"error": NOT_APPROVED_MESSAGE}

            net_by_pot = await _approval_net_by_pot(session, request)
            balances = await get_staff_balances()
            before_pots = await balances.lock_pots(session, request.employee_id)
            rows: list[dict] = []
            pot_writes: dict[str, float] = {}
            refund_reason = f"refund of {kind} #{request.item_id}"
            for pot, net in net_by_pot.items():
                key = step_key(kind, request.item_id, f"refund:ledger:{pot}", version)
                if pot == "vacation_taken":                        # reverse the counter, no pot to write
                    before_count = await _vacation_taken_for_year(session, request.employee_id, _leave_year(request))
                    await post_ledger(session, request, request.employee_id, "vacation_taken",
                                      before_count, before_count - net, refund_reason, actor, channel, key)
                    rows.append({"label": POT_LABEL["vacation_taken"],
                                 "value": f"{before_count:g} -> {before_count - net:g} days"})
                    continue
                before = before_pots.get(pot, 0.0)
                after = before - net                               # undo the approval's signed change
                pot_writes[pot] = after
                await post_ledger(session, request, request.employee_id, pot,
                                  before, after, refund_reason, actor, channel, key)
                rows.append({"label": POT_LABEL[pot], "value": f"{before:g} -> {after:g} days"})
            if pot_writes:
                await balances.write_pots(session, request.employee_id, pot_writes)

            _mark_decided(request, "refunded", actor, channel)
            request.balance_state = "reversed"
            refunded_key = step_key(kind, request.item_id, "refunded", version)
            await append_event(session, request, "refunded", channel, actor, {}, refunded_key)
            reversed_key = step_key(kind, request.item_id, "balance_reversed", version)
            event = await append_event(session, request, "balance_reversed", channel, actor, {}, reversed_key)
            note = "" if rows else "This request used no balance, so nothing was reversed."
            subject, html, sms_text = _result_context(request, "Refunded", rows, note)
            await _enqueue_result(session, request, event, version, subject, html, sms_text)
            log_step(request, "refunded", channel=channel, actor=actor)
            await session.commit()
        _wake_sender()                                             # send the refund notice without waiting
        return {"status": "refunded", "no_balance_change": not rows}


async def _approval_net_by_pot(session, request: RequestRecord) -> dict[str, float]:
    """The net change each pot moved on this request's approval, from the ledger.

    Summing the approval's ledger rows per pot gives exactly what to reverse, so
    a refund can never guess the amount.

    Args:
        session: An open session.
        request: The request being refunded.

    Returns:
        ``{pot: net_change}`` for every pot the approval touched.
    """
    rows = (await session.execute(
        select(BalanceLedger.pot, BalanceLedger.change)
        .where(BalanceLedger.request_id == request.id)
    )).all()
    net: dict[str, float] = {}
    for pot, change in rows:
        net[pot] = net.get(pot, 0.0) + float(change)               # sum, so split approvals reverse whole
    return net


def _mark_decided(request: RequestRecord, status: str, actor, channel: str) -> None:
    """Stamp the decision columns, and the SharePoint-compat flags older reads check.

    Args:
        request: The request being decided.
        status: "approved", "rejected" or "refunded".
        actor: Who decided.
        channel: Where it came from.
    """
    from app.models.mixins import utcnow
    request.status = status
    request.decided_at = utcnow()
    request.decided_by = str(actor) if actor is not None else None
    request.decided_channel = channel
    # Keep the flags the SMS route and the dispatcher's "already processed" tests
    # read, so a decided request reads as processed on either storage.
    extra = dict(request.extra or {})
    if status in ("approved", "rejected"):
        if request.kind == "leave":
            extra["ApproveProcessedFlag"] = "Processed"
        elif request.kind == "carryover_payout":
            extra["SystemState"] = "Processed"
    request.extra = extra
