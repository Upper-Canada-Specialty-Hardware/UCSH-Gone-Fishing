"""Settle an approved request that never moved a balance (#399, #400).

Employee setup flags an approved request whose ``balance_state`` is still
``not_applied``: it should have spent or banked days and nothing was recorded.
An admin settles each one with a reason, in one of three ways:

* ``now``  - take it off now: move the pots exactly as approving would have,
  using the same pure planner the decision path uses (``_plan_balance``);
* ``hand`` - already taken off by hand: someone already lowered the balance, so
  nothing moves; ledger rows record which pots the days came from, so the
  request's "Taken from" shows them and a later refund can give them back;
* ``none`` - no balance needed: the request should never have cost days (a test
  request, a closed-office day); nothing moves and nothing is recorded as taken.

Every outcome lands in one transaction: the pots (``now`` only), the ledger rows,
the request's new ``balance_state``, a timeline event and a field-change row
carrying the reason, and the matching legacy finding marked resolved. No email
or text is sent; this is bookkeeping, not a decision the employee is told about.
"""
import logging

from sqlalchemy import select

from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.mixins import utcnow
from app.models.request_record import RequestRecord
from app.repositories.staff_balances import get_staff_balances
from app.services.concurrency import lock_manager
from app.services.request_decisions import (
    _after_decision_followups,
    _append_audit_entry,
    _leave_year,
    _load,
    _plan_balance,
    _vacation_taken_for_year,
    decisions_active,
)
from app.services.request_journal import append_event, log_step, post_ledger, record_change, step_key

logger = logging.getLogger(__name__)

# The three ways to settle, as the dialog posts them.
OUTCOMES = ("now", "hand", "none")
# Balance states that mean "approved, but nothing proves a pot moved": only these can be settled.
SETTLEABLE_STATES = ("not_applied", "legacy_unknown")
# The pots a leave's days can have been taken from by hand (payout is never spent by leave).
HAND_LEAVE_POTS = ("sick", "make_up", "carry_over", "vacation")
# How close the hand amounts must add up to the request's days (float noise only).
SUM_TOLERANCE = 0.001
# Who a settle is recorded as, matching the other admin actions.
ADMIN_ACTOR = "admin"
CHANNEL = "admin_dashboard"
# The reconciliation case the importer files for the same discrepancy.
FINDING_CASE = "approved_no_balance_change"


class SettleError(Exception):
    """A settle that was refused; carries the HTTP status the route returns."""

    def __init__(self, message: str, status_code: int = 400):
        """Keep the message and status.

        Args:
            message: The plain sentence shown to the admin.
            status_code: 400 for a bad request, 404 gone, 409 already settled or wrong state.
        """
        super().__init__(message)
        self.status_code = status_code


def _num(value) -> float:
    """A Decimal/None column as a float (None reads as 0)."""
    return float(value or 0)


def _moves(before_pots: dict[str, float], moved: dict[str, float]) -> list[dict]:
    """The planned moves as a list the dialog can show, in a fixed pot order.

    Args:
        before_pots: The pots now.
        moved: ``{pot: new_value}`` for the pots approving would change.

    Returns:
        ``[{"pot", "before", "after", "change"}]``; change is signed (negative = taken).
    """
    order = ("sick", "make_up", "carry_over", "vacation", "payout")   # the spending cascade, payout last
    return [
        {"pot": pot, "before": before_pots[pot], "after": moved[pot], "change": moved[pot] - before_pots[pot]}
        for pot in order if pot in moved
    ]


def _describe(request: RequestRecord) -> dict:
    """The request facts the dialog shows at the top of each step.

    Args:
        request: The request row.

    Returns:
        A plain dict, dates as ISO strings.
    """
    return {
        "kind": request.kind,
        "item_id": request.item_id,
        "employee_id": request.employee_id,
        "employee_name": request.employee_name,
        "leave_type": request.leave_type,
        "request_option": request.request_option,
        "start_date": request.start_date.isoformat() if request.start_date else None,
        "end_date": request.end_date.isoformat() if request.end_date else None,
        "days": _num(request.days) if request.days is not None else None,
        "hours": _num(request.hours) if request.hours is not None else None,
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "decided_by": request.decided_by,
        "status": request.status,
        "balance_state": request.balance_state,
    }


def _check_settleable(request: RequestRecord | None) -> RequestRecord:
    """Refuse a request that is gone, not approved, or already settled.

    Args:
        request: The loaded row, or None.

    Returns:
        The same row when it can be settled.

    Raises:
        SettleError: 404 when gone, 409 when not approved or already moved.
    """
    if request is None:
        raise SettleError("This request no longer exists.", 404)
    if request.status != "approved":
        raise SettleError("Only approved requests can be settled.", 409)
    if request.balance_state not in SETTLEABLE_STATES:
        raise SettleError("This request is already settled.", 409)   # another admin got here first
    return request


async def settle_preview(kind: str, item_id) -> dict:
    """What each way of settling would do, read without writing anything.

    Args:
        kind: The stored request kind ("leave", "overtime", "carryover_payout").
        item_id: The request's item id.

    Returns:
        ``{"request", "pots", "plan": {"outcome", "moves", "note"}, "hand_editable"}``.
        ``plan.outcome`` is the decision path's own word: balance_applied,
        counted, no_balance_used or refused_negative.

    Raises:
        SettleError: When the request cannot be settled, or before the switch.
    """
    if not await decisions_active():
        raise SettleError("Settling needs requests and staff in Postgres.", 409)
    async with async_session() as session:
        request = _check_settleable(await _load(session, kind, item_id))
        balances = await get_staff_balances()
        try:
            pots = await balances.lock_pots(session, request.employee_id)   # a read; the session is not committed
        except LookupError:
            raise SettleError("This person has no staff record, so there is no balance to settle.", 409)
        outcome, moved, note = _plan_balance(request, pots)
        result = {
            "request": _describe(request),                         # read before the rollback expires the row
            "pots": pots,
            "plan": {"outcome": outcome, "moves": _moves(pots, moved), "note": note},
            # Only a leave that spends pots can say which pots by hand; the rest record the plan as is.
            "hand_editable": request.kind == "leave" and outcome == "balance_applied",
        }
        await session.rollback()                                   # release the row lock the read took
    return result


def _hand_moves(request: RequestRecord, pots: dict[str, float], moved: dict[str, float],
                pieces: dict[str, float] | None) -> dict[str, float]:
    """The signed change per pot that "already taken off by hand" records.

    A leave takes the admin's amounts (days taken per pot, which must add up to
    the request's days); every other kind records exactly what approving would
    have moved.

    Args:
        request: The request.
        pots: The pots now.
        moved: The planner's ``{pot: new_value}``.
        pieces: The admin's ``{pot: days taken}`` for a leave, or None to use the plan.

    Returns:
        ``{pot: signed change}`` (negative = taken out), only non-zero pots.

    Raises:
        SettleError: 400 for an unknown pot, a negative amount, or a sum that does not match.
    """
    if request.kind != "leave" or pieces is None:
        return {pot: value - pots[pot] for pot, value in moved.items() if value != pots[pot]}
    changes: dict[str, float] = {}
    for pot, amount in pieces.items():
        if pot not in HAND_LEAVE_POTS:
            raise SettleError(f"Days cannot come from {pot}.")
        amount = float(amount or 0)
        if amount < 0:
            raise SettleError("Enter the days taken as positive numbers.")
        if amount:
            changes[pot] = -amount                                 # taken out of that pot
    days = _num(request.days)
    total = -sum(changes.values())
    if abs(total - days) > SUM_TOLERANCE:                          # must account for the whole request
        raise SettleError(f"These add up to {total:g} days; the request is {days:g} days.")
    return changes


async def _resolve_finding(session, request: RequestRecord, note: str) -> None:
    """Mark the importer's open finding for this request resolved, with the reason.

    Args:
        session: The open settle session; not committed here.
        request: The settled request.
        note: The admin's reason.
    """
    rows = (await session.execute(
        select(LegacyFinding).where(
            LegacyFinding.case == FINDING_CASE,
            LegacyFinding.subject == "request",
            LegacyFinding.kind == request.kind,
            LegacyFinding.subject_id == str(request.item_id),
            LegacyFinding.status == "open",
        )
    )).scalars().all()
    for finding in rows:                                           # normally one; a variant may add more
        finding.status = "resolved"
        finding.note = note
        finding.resolved_by = ADMIN_ACTOR
        finding.resolved_at = utcnow()


async def settle(kind: str, item_id, outcome: str, reason: str,
                 pieces: dict[str, float] | None = None, actor: str = ADMIN_ACTOR) -> dict:
    """Settle one approved request that never moved a balance.

    Args:
        kind: The stored request kind.
        item_id: The request's item id.
        outcome: "now", "hand" or "none" (see the module docstring).
        reason: Why, required; kept on the timeline and the field change.
        pieces: For a "hand" leave, ``{pot: days taken}``; None uses the plan.
        actor: Who settled it.

    Returns:
        ``{"status": "settled", "outcome", "balance_state", "moves"}``.

    Raises:
        SettleError: A bad outcome or reason, a refused request, or a plan that
            cannot be applied (a carry-over that would take vacation below zero).
    """
    reason = (reason or "").strip()
    if outcome not in OUTCOMES:
        raise SettleError("Choose how this request was settled.")
    if not reason:
        raise SettleError("A reason is required.")
    if not await decisions_active():
        raise SettleError("Settling needs requests and staff in Postgres.", 409)

    async with async_session() as session:
        request = _check_settleable(await _load(session, kind, item_id))
        async with lock_manager.lock(request.employee_id):          # one person's balance writes, one at a time
            await session.refresh(request, with_for_update=True)  # see a settle another admin just committed
            _check_settleable(request)
            old_state = request.balance_state
            version = request.approval_version
            balances = await get_staff_balances()
            try:
                pots = await balances.lock_pots(session, request.employee_id)
            except LookupError:
                raise SettleError("This person has no staff record, so there is no balance to settle.", 409)
            plan, moved, _note = _plan_balance(request, pots)
            if outcome != "none" and plan == "refused_negative":
                raise SettleError("Taking this off now would put vacation below zero. "
                                  "Settle it by hand or as no balance needed.", 409)

            # A settle that moves nothing for its kind reads as "no balance needed".
            effective = outcome
            if outcome != "none" and plan == "no_balance_used":
                effective = "none"
            if effective == "hand" and plan == "counted":
                effective = "now"                                  # the hourly counter is the ledger: nothing to do by hand

            label = {"now": "taken off now", "hand": "already taken off by hand", "none": "no balance needed"}[effective]
            ledger_reason = f"settled {kind} #{request.item_id}, {label}: {reason}"
            shown: list[dict] = []                                 # what the dialog's summary lists
            final_pots = dict(pots)

            if effective == "now" and plan == "counted":           # hourly vacation: add to the counter
                year = _leave_year(request)
                before_count = await _vacation_taken_for_year(session, request.employee_id, year)
                days = _num(request.days)
                key = step_key(kind, request.item_id, "settle:ledger:vacation_taken", version)
                await post_ledger(session, request, request.employee_id, "vacation_taken",
                                  before_count, before_count + days, ledger_reason, actor, CHANNEL, key)
                shown.append({"pot": "vacation_taken", "before": before_count, "after": before_count + days,
                              "change": days})
                new_state, event_type = "counted", "counted"
            elif effective == "now":                               # move the pots the way approval would
                if moved:
                    await balances.write_pots(session, request.employee_id, moved)
                for pot, after_value in moved.items():
                    key = step_key(kind, request.item_id, f"settle:ledger:{pot}", version)
                    await post_ledger(session, request, request.employee_id, pot,
                                      pots[pot], after_value, ledger_reason, actor, CHANNEL, key)
                shown = _moves(pots, moved)
                final_pots.update(moved)
                _append_audit_entry(request, "approve", "Settled: taken off now", pots, moved)
                new_state, event_type = "applied", "balance_applied"
            elif effective == "hand":                              # record only: the balance already reflects it
                changes = _hand_moves(request, pots, moved, pieces)
                if not changes:
                    raise SettleError("Enter where the days were taken from.")
                before_display = {}
                for pot, change in changes.items():
                    before = pots[pot] - change                    # what it was before the hand edit
                    before_display[pot] = before
                    key = step_key(kind, request.item_id, f"settle:hand:{pot}", version)
                    await post_ledger(session, request, request.employee_id, pot,
                                      before, pots[pot], ledger_reason, actor, CHANNEL, key)
                    shown.append({"pot": pot, "before": before, "after": pots[pot], "change": change})
                _append_audit_entry(request, "approve", "Settled: already taken off by hand",
                                    before_display, {pot: pots[pot] for pot in changes})
                new_state, event_type = "applied", "balance_applied"
            else:                                                  # no balance needed: nothing moves
                new_state, event_type = "no_balance", "no_balance_used"

            request.balance_state = new_state
            record_change(session, request, "balance_state", old_state, new_state, actor, CHANNEL, reason)
            key = step_key(kind, request.item_id, f"settle:{event_type}", version)
            await append_event(session, request, event_type, CHANNEL, actor,
                               {"settled": effective, "reason": reason}, key)
            await _resolve_finding(session, request, reason)
            log_step(request, "settled", outcome=effective, state=new_state)
            await session.commit()

    if effective == "now" and moved:
        # Recalc the allow date from the new pots; settling strands nothing new, so no notice.
        await _after_decision_followups(
            request, final_pots.get("vacation", 0.0), final_pots.get("carry_over", 0.0),
            send_blocked_notice=False,
        )
    return {"status": "settled", "outcome": effective, "balance_state": new_state, "moves": shown}
