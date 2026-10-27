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
from app.services.app_settings import toronto_today  # Toronto calendar day for the booking policy (#407)
from app.services.balance import (
    booking_refusal,
    carryover_available_for,
    cascade_current_year_pure,
    simulate_carryover_payout_impact,
    simulate_leave_impact,
    simulate_overtime_impact,
)
from app.services.concurrency import lock_manager
from app.services.employment import (
    HOURLY_TYPES,
    NO_COST_LEAVE,
    PARTIAL_DAY,
    SICK_LEAVE,
    VACATION,
    leave_costs_balance,
    overtime_costs_balance,
    whole_sick_days,
)
from app.services.overlap_detection import approval_refusal, find_leave_conflict, find_overtime_conflict
from app.services.request_journal import append_event, enqueue_message, log_step, post_ledger, step_key
from app.templates_render import render_request_result

logger = logging.getLogger(__name__)

# A rejection reason is capped at this many characters at every entry point (the
# dashboard routes, the email approval page and an SMS reply), so an unbounded
# reason cannot be typed (finding 14). The decision path stores and emails it,
# and a result text carries it, so the cap keeps the SMS within Twilio's limit.
MAX_REJECT_REASON_LEN = 500

# Twilio refuses a message body over 1600 characters, and that refusal is now a
# permanent failure (finding 7), so the rejection RESULT text is capped here. The
# email keeps the full reason; only the text is shortened (finding 14).
_SMS_BODY_MAX = 1600


def _truncate_sms(text: str) -> str:
    """Cap a result text at Twilio's 1600-character limit, marking any cut.

    Args:
        text: The full SMS body.

    Returns:
        The text unchanged when within the limit, else trimmed to fit with a
        trailing "..." so the recipient sees it was shortened.
    """
    if len(text) <= _SMS_BODY_MAX:
        return text                                                # already within the limit
    return text[: _SMS_BODY_MAX - 3].rstrip() + "..."              # trim and mark the cut

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
# What a manager sees acting on a request its owner cancelled (#519): the old
# approve and reject links stop working, and say why instead of "out of date".
CANCELLED_MESSAGE = "This request was cancelled by the person who asked for it. Nothing is needed from you."


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
        after = simulate_leave_impact(sp, leave_type, days, carryover_available=_carryover_available(request))
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


def _carryover_available(request: RequestRecord) -> bool:
    """Whether carry-over may pay this request, from its last day (#407).

    Carry-over pays none of a request whose last day is after the carry-over
    expiry; the cascade then skips carry-over and the shortfall lands on
    Vacation instead. Replaces the old next-year test, which picked a different
    cascade and let carry-over go negative.

    Args:
        request: The leave request.

    Returns:
        True when the request's last day is on or before the carry-over expiry,
        or when it has no dates.
    """
    last_day = request.end_date or request.start_date      # the latest day decides
    return carryover_available_for(last_day, toronto_today())


def _booking_refusal(request: RequestRecord, before_pots: dict[str, float]) -> str | None:
    """The booking-limit refusal for a salaried Vacation/part-day approval (#407).

    Computed from the pots read under the balance lock, so the dates are checked
    against the same balances the deduction would use. Returns None for every
    other kind, leave type, or hourly person, and when the dates are within the
    limit.

    Args:
        request: The request being approved.
        before_pots: The employee's pots before the decision.

    Returns:
        The refusal sentence, or None when the request is allowed.
    """
    if request.kind != "leave":                            # only leave is booked against the limit
        return None
    sp = _sp_fields(before_pots, request)                  # balances the refusal reads V and C from
    last_day = request.end_date or request.start_date      # the day the limit is checked against
    return booking_refusal(sp, request.leave_type or "", last_day, toronto_today())


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


async def _decider_name(session, request: RequestRecord) -> str | None:
    """The name of the manager who actually decided, when it is not the assigned one.

    Any manager on the person's list can decide from their own dashboard or link,
    and since #342 the assigned manager may be a stand-in, so the decider is read
    from the recorded actor rather than assumed to be ``manager_name`` (#371).

    The name is read with the session already in hand (a direct ``Staff`` select
    by id), not a second ``staff_store`` call: this path runs only once staff live
    in Postgres (``decisions_active``), so the id resolves there, and opening a
    second session while the decision transaction holds the request row risks a
    connection-pool stall under load (finding for #371). Any failure returns None,
    so a missing name never fails the decision.

    Args:
        session: The open decision (or read) session; reused for the lookup.
        request: The decided request, with ``decided_by`` set.

    Returns:
        The decider's staff name, or None when the decider is the assigned
        manager, an admin, or cannot be found (the caller then falls back).
    """
    actor = (request.decided_by or "").strip()
    if not actor.isdigit() or actor == str(request.manager_id or ""):
        return None                                                # admin, unknown, or the assigned manager
    try:
        from app.models.staff import Staff                         # local: keeps this module's imports light
        row = (await session.execute(
            select(Staff.name).where(Staff.sp_item_id == actor)    # the decider's staff row by id
        )).first()
    except Exception:  # noqa: BLE001 - a name lookup must never block the result message
        logger.exception("Decider lookup failed for %s #%s", request.kind, request.item_id)
        return None
    name = (row[0] if row else "") or ""                           # the decider's display name
    return name.strip() or None


def _decider_label(request: RequestRecord, decider_name: str | None = None) -> tuple[str | None, str | None]:
    """Who decided, as a bare (role, name) pair the headings and phrases share.

    One place to read the decision's actor and channel, so ``_decider_phrase``
    and the rejection headings (#434) can never disagree on who acted. An admin
    decision or refund (actor ``"admin"``, or the ``admin_dashboard`` channel) is
    HR; a deciding manager (a stand-in named by ``decider_name``, or the assigned
    ``manager_name``) is that manager; otherwise neither is known.

    Args:
        request: The decided request, with ``decided_by`` / ``decided_channel`` set.
        decider_name: The deciding manager's name when they are not the assigned
            manager (from ``_decider_name``), else None.

    Returns:
        ``("admin", "HR")``, ``("manager", <name>)``, or ``(None, None)``.
    """
    actor = (request.decided_by or "").strip()                     # the recorded actor id/name
    channel = request.decided_channel or ""                        # where the decision came from
    if actor.lower() == "admin" or channel == "admin_dashboard":   # an admin acted, not the manager
        return "admin", "HR"
    if decider_name:                                               # another manager on the list decided
        return "manager", decider_name
    if request.manager_name:                                       # the assigned manager decided
        return "manager", request.manager_name
    return None, None                                              # no decider is known


def _decider_phrase(request: RequestRecord, decider_name: str | None = None) -> str:
    """The " by <who>" clause for a result message, naming who actually decided.

    An admin decision or refund is named as an administrator rather than the
    request's assigned manager (finding 10); a manager deciding through their own
    channel keeps naming the manager. This changes only the wording, never who
    the result is sent to.

    Args:
        request: The decided request, with ``decided_by`` / ``decided_channel`` set.
        decider_name: The deciding manager's name when they are not the assigned
            manager (from ``_decider_name``), else None.

    Returns:
        ``" by <name>"`` when a decider is known, or ``""`` when none is.
    """
    role, who = _decider_label(request, decider_name)              # the shared who-decided read
    if role == "admin":                                            # an admin acted, not the manager
        return " by an administrator"
    if role == "manager":                                          # a manager (stand-in or assigned) decided
        return f" by {who}"
    return ""


def _result_context(
    request: RequestRecord, outcome_word: str, rows: list[dict], note: str,
    decider_name: str | None = None, reason: str | None = None,
) -> tuple[str, str, str]:
    """Build the result email subject, HTML body and SMS text from the step.

    A rejection carrying a reason (#434) takes one of two forms, each with the
    reason: the supervisor form ("<Manager> did not approve your <kind>
    request") or the admin form ("HR did not approve your <kind> request", plus
    "If you have questions, contact HR."). A rejection with no reason (an SMS
    reject with no words) falls through to the plain "was not approved" sentence,
    unchanged. Approved and Refunded are unchanged.

    Args:
        request: The decided request (for the name, kind and number).
        outcome_word: "Approved", "Rejected" or "Refunded".
        rows: Balance lines ``[{"label", "value"}]`` for the email table.
        note: A closing sentence, or "".
        decider_name: The deciding manager's name when not the assigned one.
        reason: The rejection reason, or None/blank for the plain fallback.

    Returns:
        ``(subject, html, sms_text)``.
    """
    name = request.employee_name or "there"
    label = KIND_LABEL.get(request.kind, "Request")
    kind_word = label.lower()                                      # "leave", "overtime", "carry over / payout"
    if outcome_word == "Rejected" and (reason or "").strip():      # the two reason-carrying forms (#434)
        return _rejected_context(request, name, label, kind_word, reason.strip(), decider_name)
    heading = f"{label} Request: {outcome_word}"
    decided_by = _decider_phrase(request, decider_name)
    verb = {"Approved": "has been approved", "Rejected": "was not approved", "Refunded": "has been refunded"}[outcome_word]
    intro = f"Hi {name}, your {kind_word} request #{request.item_id} {verb}{decided_by}."
    subject = f"{request.employee_name or 'Staff'} - {label} Request: {outcome_word}"
    html = render_request_result(heading, intro, rows, note)
    # The text says the same in one plain line, with each balance line appended.
    text_lines = [f"{label} request #{request.item_id} {verb}{decided_by}."]
    text_lines += [f"{row['label']}: {row['value']}" for row in rows]
    if note:
        text_lines.append(note)
    return subject, html, " ".join(text_lines)


def _rejected_context(
    request: RequestRecord, name: str, label: str, kind_word: str, reason: str, decider_name: str | None,
) -> tuple[str, str, str]:
    """Build a rejection email/text that names who refused and gives the reason (#434).

    Two forms from one shell: HR (an admin decision) adds a "contact HR" line; a
    manager is named by name. The reason always shows on its own line. When
    neither decider is known the plain "was not approved" heading is used, still
    with the reason.

    Args:
        request: The rejected request (for its number).
        name: The employee's first name or "there", for the greeting.
        label: The kind in title case, for the subject.
        kind_word: The kind in lower case, for the sentence.
        reason: The non-blank reason the request was turned down.
        decider_name: The deciding manager's name when not the assigned one.

    Returns:
        ``(subject, html, sms_text)``.
    """
    role, who = _decider_label(request, decider_name)              # HR, a manager, or neither
    footer = None                                                  # only the admin form carries the HR line
    if role == "admin":                                            # HR did not approve it
        heading = f"HR did not approve your {kind_word} request"
        line = f"HR did not approve your {kind_word} request #{request.item_id}."
        footer = "If you have questions, contact HR."
    elif role == "manager":                                        # a named manager did not approve it
        heading = f"{who} did not approve your {kind_word} request"
        line = f"{who} did not approve your {kind_word} request #{request.item_id}."
    else:                                                          # decider unknown: plain wording, still with reason
        heading = f"{label} Request: Rejected"
        line = f"Your {kind_word} request #{request.item_id} was not approved."
    intro = f"Hi {name}, {line}"                                   # the email greets, the text does not
    subject = f"{request.employee_name or 'Staff'} - {label} Request: Rejected"
    html = render_request_result(heading, intro, [], note="", reason=reason, footer=footer)
    # The text says the same in one line: the sentence, the reason, then the HR
    # line. Capped to Twilio's limit so an over-long reason cannot make the result
    # text permanently unsendable; the email above keeps the full reason (finding 14).
    text_lines = [line, f"Reason: {reason}"]
    if footer:
        text_lines.append(footer)
    return subject, html, _truncate_sms(" ".join(text_lines))


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


def _append_audit_entry(request: RequestRecord, action: str, operation: str,
                        before_pots: dict[str, float], after_pots: dict[str, float]) -> None:
    """Add one entry to the request's BalanceAuditLog, as the SharePoint path did.

    The ledger is the record of what moved; this copy is for display only. The
    admin requests screen ("What it did to balances", "Audit trail") and the
    employee's request history read BalanceAuditLog, so without it every request
    decided after the switch would show no balance change. Refunds never read it
    back for a Postgres-decided request (they reverse the ledger rows).

    Args:
        request: The request being decided; its ``extra`` gets the log.
        action: "approve" or "refund", the entry's action.
        operation: The step's plain label, shown on the screen.
        before_pots: Pot values before, keyed by pot name.
        after_pots: Pot values after, for the pots that moved only.
    """
    import json
    from app.services.audit_trail import AuditTrailBuilder   # lazy: keeps this module's imports light
    if not after_pots:
        return                                                     # nothing moved: no entry, as before
    builder = AuditTrailBuilder(action)
    builder.add_step(
        operation,
        {POT_COLUMNS[pot]: before_pots.get(pot, 0.0) for pot in after_pots},   # SharePoint column names
        {POT_COLUMNS[pot]: value for pot, value in after_pots.items()},
    )
    extra = dict(request.extra or {})
    try:
        entries = json.loads(extra.get("BalanceAuditLog") or "[]")  # an imported row may already have some
    except (TypeError, ValueError):
        entries = []                                               # unreadable old log: start a fresh one
    if not isinstance(entries, list):
        entries = []
    entries.append(builder.build_entry())
    extra["BalanceAuditLog"] = json.dumps(entries)
    request.extra = extra                                          # a new dict so the JSON change is saved


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
        if request.status == "cancelled":                          # its owner cancelled it (#519)
            await _refuse(session, request, channel, actor, version, "cancelled", CANCELLED_MESSAGE)
            await session.commit()
            return {"error": CANCELLED_MESSAGE, "refused": "cancelled"}
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
            try:
                before_pots = await balances.lock_pots(session, request.employee_id)
            except LookupError:
                # An imported request whose submitter never matched a staff row
                # (employee_id like "spuser:N"/"spitem:N") has no balance to
                # move: refuse cleanly, as the SharePoint path returns "Employee
                # not found", instead of 500ing on the missing lookup.
                logger.warning("No staff row for %s #%s (employee %s); cannot approve",
                               kind, request.item_id, request.employee_id)
                return {"error": "Employee not found"}
            # Checked after the balance lock so a clashing approval committed a
            # moment ago (on any server) is seen; nothing is written on a clash.
            clash = await _approval_conflict(session, request)
            if clash:                                              # the manager rejects or cancels the other one
                await _refuse(session, request, channel, actor, version, "conflict", clash)
                await session.commit()
                return {"error": clash, "refused": "conflict"}
            # Booking-limit refusal (#407): a salaried Vacation/part-day whose last
            # day is past what the person may book is refused against the balances
            # at this moment. Nothing is written; the request stays Pending and the
            # approver sees the message, exactly like the clash refusal above.
            booking = _booking_refusal(request, before_pots)
            if booking:
                await _refuse(session, request, channel, actor, version, "booking_limit", booking)
                await session.commit()
                return {"error": booking, "refused": "booking_limit"}
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
                _append_audit_entry(request, "approve", "Approved", before_pots, moved)   # for the balance panel
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
            decider_name = await _decider_name(session, request)    # who really decided (#371)
            subject, html, sms_text = _result_context(request, "Approved", rows, note, decider_name)
            await _enqueue_result(session, request, event, version, subject, html, sms_text)
            log_step(request, "approved", channel=channel, actor=actor, outcome=event_type)
            # The pots after this approval (moved holds the new values; empty for
            # a counted or no-balance outcome), for the RequestAllowDate recalc.
            final_pots = {**before_pots, **moved}
            if request.kind == "carryover_payout" and moved:
                # The "New balance" the admin requests table and export show, in
                # the same text the SharePoint path wrote, so old and new rows read alike.
                extra = dict(request.extra or {})
                extra["NewBalance"] = (
                    f"{{Vacation:{final_pots.get('vacation', 0.0)}, "
                    f"CarryOver:{final_pots.get('carry_over', 0.0)}, "
                    f"Payout:{final_pots.get('payout', 0.0)}}}"
                )
                request.extra = extra                              # a new dict so the JSON change is saved
            await session.commit()
        _wake_sender()                                             # send the result without waiting for the poll
        # Recalc the allow date, and (leave/overtime) tell the employee about any
        # request this approval has just stranded. After the commit, best-effort.
        await _after_decision_followups(
            request, final_pots.get("vacation", 0.0), final_pots.get("carry_over", 0.0),
            send_blocked_notice=True,
        )
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
        if request.status == "cancelled":                          # its owner cancelled it (#519)
            await _refuse(session, request, channel, actor, version, "cancelled", CANCELLED_MESSAGE)
            await session.commit()
            return {"error": CANCELLED_MESSAGE, "refused": "cancelled"}
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
            note = reason or "The request was not approved."       # the no-reason fallback sentence
            decider_name = await _decider_name(session, request)    # who really decided (#371)
            # Pass the raw reason too: with one, _result_context builds the
            # supervisor/HR form (#434); with none, it uses the note above.
            subject, html, sms_text = _result_context(request, "Rejected", [], note, decider_name, reason=reason)
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

    A request approved in SharePoint before this ledger existed (legacy) has no
    ledger rows to reverse. Rather than restore nothing, its balance effect is
    re-derived the way the SharePoint refund does (``_legacy_refund_net``) and
    given back, with ledger rows whose reason says the amount was reconstructed
    (finding 8). The result email shows the restored amounts either way.

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
            reconstructed = False
            # Legacy: no ledger rows for this approval. A request an admin settled as
            # "no balance needed" (#399) also has none, and must stay that way: never
            # re-derive days for it, or the refund would give back days never taken.
            if not net_by_pot and request.balance_state != "no_balance":
                net_by_pot = _legacy_refund_net(request)           # re-derive what it moved, as the SharePoint path does
                reconstructed = bool(net_by_pot)                   # empty still means a genuine no-balance approval
            balances = await get_staff_balances()
            try:
                before_pots = await balances.lock_pots(session, request.employee_id)
            except LookupError:
                # An imported request whose submitter never matched a staff row:
                # nothing to reverse, refuse cleanly like the SharePoint path.
                logger.warning("No staff row for %s #%s (employee %s); cannot refund",
                               kind, request.item_id, request.employee_id)
                return {"error": "Employee not found"}
            rows: list[dict] = []
            pot_writes: dict[str, float] = {}
            # Carry-over the approval spent below zero does not go back to carry-over
            # when the shortfall is already clear; it is restored to vacation instead
            # (see the split below). Captured here, applied once after the loop.
            carryover_to_vacation = 0.0
            carryover_before = await _approval_carryover_before(session, request)  # carry-over before the approval
            refund_reason = (
                f"refund of {kind} #{request.item_id} (amount reconstructed, approved before the ledger)"
                if reconstructed else
                f"refund of {kind} #{request.item_id}"
            )
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
                if pot == "carry_over" and net < 0:                # the approval spent carry-over: split the refund
                    # A spend that drove carry-over below zero (the old next-year path)
                    # must not come back as carry-over the setup fix already moved on
                    # to vacation. Give back the real carry-over spent, then the part
                    # that went below zero only up to today's shortfall; the rest to
                    # vacation (#407 review).
                    spent = -net                                   # days the approval took from carry-over
                    real = min(spent, max(carryover_before, 0.0))  # part that was actual positive carry-over
                    borrowed = spent - real                        # part that drove carry-over below zero
                    to_carryover = min(borrowed, max(-before, 0.0))  # cover only the shortfall carry-over has now
                    give_c = real + to_carryover                   # total returned to carry-over
                    carryover_to_vacation = borrowed - to_carryover  # the remainder is restored to vacation
                    after = before + give_c
                    reason = (f"{refund_reason} (carry-over split: {give_c:g} to carry-over, "
                              f"{carryover_to_vacation:g} to vacation)")
                    pot_writes[pot] = after
                    await post_ledger(session, request, request.employee_id, pot,
                                      before, after, reason, actor, channel, key)
                    rows.append({"label": POT_LABEL[pot], "value": f"{before:g} -> {after:g} days"})
                    continue
                after = before - net                               # undo the approval's signed change
                pot_writes[pot] = after
                await post_ledger(session, request, request.employee_id, pot,
                                  before, after, refund_reason, actor, channel, key)
                rows.append({"label": POT_LABEL[pot], "value": f"{before:g} -> {after:g} days"})
            if carryover_to_vacation:                              # carry-over that went below zero lands on vacation
                v_before = pot_writes.get("vacation", before_pots.get("vacation", 0.0))
                v_after = v_before + carryover_to_vacation
                key = step_key(kind, request.item_id, "refund:carryover-to-vacation", version)
                await post_ledger(session, request, request.employee_id, "vacation", v_before, v_after,
                                  f"{refund_reason} (carry-over that went below zero, restored to vacation)",
                                  actor, channel, key)
                pot_writes["vacation"] = v_after                   # fold the restore into the single write
                rows.append({"label": POT_LABEL["vacation"], "value": f"{v_before:g} -> {v_after:g} days"})
            # Reversing the ledger can leave a pot negative: refunding banked
            # overtime after it was spent takes Make-Up below zero. Cascade the
            # result the way the SharePoint refund does, and write and ledger the
            # extra moves, so Make-Up/Carry Over never sit negative (finding 6).
            pots_after = {**before_pots, **pot_writes}
            for pot, new_value in _refund_cascade(request, pots_after).items():
                if new_value == pots_after[pot]:
                    continue                                       # this pot did not move in the cascade
                key = step_key(kind, request.item_id, f"refund:cascade:{pot}", version)
                await post_ledger(session, request, request.employee_id, pot,
                                  pots_after[pot], new_value, f"{refund_reason} (cascade)", actor, channel, key)
                rows.append({"label": POT_LABEL[pot], "value": f"{pots_after[pot]:g} -> {new_value:g} days"})
                pot_writes[pot] = new_value                        # fold the cascade move into the single write
            if pot_writes:
                await balances.write_pots(session, request.employee_id, pot_writes)
                _append_audit_entry(request, "refund", "Refunded", before_pots, pot_writes)   # for the balance panel
            # The pots after the reversal and cascade, for the RequestAllowDate recalc below.
            final_pots = {**before_pots, **pot_writes}

            _mark_decided(request, "refunded", actor, channel)
            request.balance_state = "reversed"
            refunded_key = step_key(kind, request.item_id, "refunded", version)
            await append_event(session, request, "refunded", channel, actor, {}, refunded_key)
            reversed_key = step_key(kind, request.item_id, "balance_reversed", version)
            event = await append_event(session, request, "balance_reversed", channel, actor, {}, reversed_key)
            note = "" if rows else "This request used no balance, so nothing was reversed."
            decider_name = await _decider_name(session, request)    # who really decided (#371)
            subject, html, sms_text = _result_context(request, "Refunded", rows, note, decider_name)
            await _enqueue_result(session, request, event, version, subject, html, sms_text)
            log_step(request, "refunded", channel=channel, actor=actor)
            await session.commit()
        _wake_sender()                                             # send the refund notice without waiting
        # A refund frees dates, so it strands nothing: recalc the allow date only.
        await _after_decision_followups(
            request, final_pots.get("vacation", 0.0), final_pots.get("carry_over", 0.0),
            send_blocked_notice=False,
        )
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


async def _approval_carryover_before(session, request: RequestRecord) -> float:
    """The carry-over value just before this request's approval first touched it.

    The refund's carry-over split needs to know how much real (positive)
    carry-over the approval actually spent versus how much it drove below zero.
    That is the ``before`` of the earliest carry-over ledger row this request
    wrote, so the earliest row's before value is read here (#407 review).

    Args:
        session: An open session.
        request: The request being refunded.

    Returns:
        The carry-over the approval started from, or 0.0 when it wrote no
        carry-over row (a legacy/reconstructed refund never spends carry-over).
    """
    before = (await session.execute(
        select(BalanceLedger.before)
        .where(BalanceLedger.request_id == request.id, BalanceLedger.pot == "carry_over")
        .order_by(BalanceLedger.id)                                # earliest first: the pre-approval value
        .limit(1)
    )).scalar_one_or_none()
    return float(before) if before is not None else 0.0


def _refund_cascade(request: RequestRecord, pots: dict[str, float]) -> dict[str, float]:
    """The cascaded values of the current-year pots after a refund's raw reversal.

    The SharePoint refund cascades the balances after putting the amount back, so
    a reversal that drives Make-Up (or Sick/Carry Over) below zero overflows down
    Sick -> Make-Up -> Carry Over -> Vacation instead of leaving a pot negative
    (finding 6). There is one cascade for every date now (#407); when the leave
    ran past the carry-over expiry the carry-over step is skipped, matching how
    its approval was applied. Pure: it only reads the reversed pots and returns
    the cascaded ones.

    Args:
        request: The request being refunded (its dates pick carry-over's role).
        pots: The pots right after the raw reversal (``{pot: value}``).

    Returns:
        ``{pot: value}`` for the pots the cascade touches; Payout and the
        ``vacation_taken`` counter are not part of the cascade and are left out.
    """
    sick = pots.get("sick", 0.0)
    make_up = pots.get("make_up", 0.0)
    carry_over = pots.get("carry_over", 0.0)
    vacation = pots.get("vacation", 0.0)
    # Leave past the carry-over expiry skipped carry-over on approval; every other
    # kind/date uses the normal chain. Non-leave always keeps carry-over available.
    carryover_available = _carryover_available(request) if request.kind == "leave" else True
    result = cascade_current_year_pure(sick, make_up, carry_over, vacation, carryover_available)
    out: dict[str, float] = {}
    for sp_field, value in result.items():                         # map SharePoint columns back to pot names
        pot = SP_FIELD_TO_POT.get(sp_field)
        if pot is not None:
            out[pot] = float(value)
    return out


def _legacy_refund_net(request: RequestRecord) -> dict[str, float]:
    """Re-derive what a pre-ledger approval moved, so a legacy refund can give it back.

    A request approved in SharePoint before this ledger existed has no ledger
    rows, so ``_approval_net_by_pot`` returns nothing and a plain refund would
    restore nothing (finding 8). This reconstructs the balance effect the same
    way the SharePoint refund's no-audit-log fallback does (the fallback branches
    in leave_requests / overtime_requests / carryover_payout), in the same sign
    convention as ``_approval_net_by_pot``: the signed change the approval made,
    which the refund loop then reverses. Hourly vacation is a ``vacation_taken``
    counter, reconstructed the same way.

    Pure (no I/O), from the request's own stored fields. ``employment_type`` is
    the snapshot on the request; it is null for legacy rows, which correctly reads
    as salaried, since the pre-ledger era ran every request under salaried rules.

    Args:
        request: The approved request being refunded, with no ledger rows.

    Returns:
        ``{pot: net_change}`` the approval is taken to have made, or ``{}`` when
        the approval genuinely moved no balance (bereavement, jury duty, hourly
        overtime), where there is nothing to restore.
    """
    hourly = _is_hourly(request)
    days = float(request.days or 0)
    if request.kind == "leave":
        leave_type = request.leave_type or ""
        if leave_type in NO_COST_LEAVE:                            # never cost anything: nothing to restore
            return {}
        if hourly:
            if leave_type in (SICK_LEAVE, PARTIAL_DAY):            # hourly: whole sick days came off sick
                return {"sick": -whole_sick_days(days)}
            if leave_type == VACATION:                             # hourly vacation was counted, not spent
                return {"vacation_taken": days}
            return {}                                              # any other hourly leave cost nothing
        if leave_type in (VACATION, PARTIAL_DAY):                  # salaried fallback: days came off Make-Up
            return {"make_up": -days}
        if leave_type == SICK_LEAVE:                               # salaried sick: days came off Sick
            return {"sick": -days}
        return {"make_up": -days}                                 # any other costing leave: Make-Up, as the fallback
    if request.kind == "overtime":
        if hourly:                                                 # hourly banks nothing, so nothing to reverse
            return {}
        return {"make_up": float(request.hours or 0) / 8}         # salaried banked hours/8 into Make-Up
    # carry-over / payout: days left Vacation and landed in Carry Over or Payout.
    if (request.request_option or "") == "payout":
        return {"vacation": -days, "payout": days}
    return {"vacation": -days, "carry_over": days}


async def _after_decision_followups(
    request: RequestRecord, final_vacation: float, final_carryover: float, *, send_blocked_notice: bool,
) -> None:
    """Run the SharePoint path's post-decision side effects on the Postgres path (finding 11).

    After a decision commits, the SharePoint path recalculates the employee's
    RequestAllowDate from the vacation/carry-over pair and, on an approval that
    reserves dates (leave, overtime), tells the employee about any other request
    that approval has just stranded. The Postgres decision path dropped both; this
    adds them back. Everything here runs after the commit, is best-effort and
    logged, and can never undo the decision: a failure is swallowed.

    Args:
        request: The decided request.
        final_vacation: The vacation pot after the decision.
        final_carryover: The carry-over pot after the decision.
        send_blocked_notice: True on an approval of leave/overtime (which reserve
            dates); False on a refund and on carry-over/payout.
    """
    try:
        # recalculate_request_allow_date writes through the staff store, which is
        # Postgres on this path, so no SharePoint call is made.
        from app.services.balance import recalculate_request_allow_date
        await recalculate_request_allow_date(request.employee_id, final_vacation, final_carryover)
    except Exception:  # noqa: BLE001 - the decision is already committed; never fail it here
        logger.exception("RequestAllowDate recalc failed after %s #%s", request.kind, request.item_id)
    if not send_blocked_notice or request.kind not in ("leave", "overtime"):
        return                                                     # only a date-reserving approval strands others
    try:
        from app.repositories.postgres.typed_requests import TypedRequestList
        from app.services.notify_blocked import notify_requests_blocked_by_approval
        fields = TypedRequestList(request.kind)._item_shape(request)["fields"]   # the approved row, SharePoint shape
        await notify_requests_blocked_by_approval(
            request.kind, request.item_id, fields, request.employee_email or "",
            employee_name=request.employee_name,                   # the blocked requests' own thread subject (#520)
        )
    except Exception:  # noqa: BLE001 - a notice failure must never undo the approval
        logger.exception("Blocked-request notice failed after %s #%s", request.kind, request.item_id)


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
