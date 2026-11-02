"""The request state machine: the states, the legal moves, and how to move (#455).

The backend owns each request's position in its lifecycle and the frontend only
renders it. ``RequestRecord.state`` is that position, stored lower snake case and
pinned to ``request_vocab.STATES`` by a CHECK constraint. It sits alongside the
older ``status`` column (pending/approved/...), which is kept untouched for
compatibility; ``state`` is the authoritative lifecycle position from here on.

Only ``waiting_on_approval`` can be decided: a request still being prepared
(``getting_ready``) or one whose preparation failed (``needs_attention``) is not
yet a thing a manager may approve or reject. Every Postgres write path moves the
state through :func:`move`, which refuses an illegal move loudly, so a bad
transition is a bug caught at once rather than a silently wrong request.
"""
import logging

from app.models.request_vocab import STATES

logger = logging.getLogger(__name__)

# The only state a request may be approved or rejected from.
DECIDABLE_STATE = "waiting_on_approval"

# A decided or imported ``status`` -> the ``state`` it corresponds to. Shared by
# the create path, the decision path and the migration backfill, so a status and
# its state can never disagree. "pending" alone is ambiguous (a pending request
# may be getting ready, waiting, or needing attention), so it is not listed here.
STATUS_TO_STATE = {
    "approved": "approved",
    "rejected": "rejected",
    "refunded": "refunded",
    "cancelled": "cancelled",
}

# from_state -> the set of states it may move to. A final state maps to an empty
# set. Mirrors the allowed moves agreed for #455:
#   getting_ready      -> waiting_on_approval | rejected (auto-reject at submit)
#                         | approved (created-approved paths) | needs_attention
#   needs_attention    -> waiting_on_approval (processed again) | getting_ready
#   waiting_on_approval-> approved | rejected | cancelled
#                         | waiting_on_approval (an admin edit: a fresh version)
#   approved           -> refunded | cancelled
#   rejected/refunded/cancelled are final (today's code never reopens them).
ALLOWED_MOVES: dict[str, set[str]] = {
    "getting_ready": {"waiting_on_approval", "rejected", "approved", "needs_attention"},
    "needs_attention": {"waiting_on_approval", "getting_ready"},
    "waiting_on_approval": {"approved", "rejected", "cancelled", "waiting_on_approval"},
    "approved": {"refunded", "cancelled"},
    "rejected": set(),
    "refunded": set(),
    "cancelled": set(),
}

# The state a brand-new row starts in before the backend has prepared it.
INITIAL_STATE = "getting_ready"


class IllegalStateMove(ValueError):
    """Raised when a state move is not allowed from the current state."""


def can_decide(record) -> bool:
    """Whether a request may be approved or rejected right now.

    Args:
        record: A request row (anything with a ``state`` attribute).

    Returns:
        True only when the request is ``waiting_on_approval``.
    """
    return (getattr(record, "state", None) or INITIAL_STATE) == DECIDABLE_STATE   # the one decidable state


def move(record, to_state: str, *, reason: str | None = None) -> str:
    """Move a request to a new state, refusing an illegal transition.

    Reads the record's current state, checks the move is in ``ALLOWED_MOVES``,
    and writes the new state onto the record (the caller's session commits it).
    Nothing else about the request changes here.

    Args:
        record: The request row; its ``state`` is read and then written in place.
        to_state: The state to move to (one of ``request_vocab.STATES``).
        reason: An optional note for the error message, naming why the move was
            attempted; it is not stored.

    Returns:
        The new state (``to_state``), for convenience.

    Raises:
        IllegalStateMove: When ``to_state`` is not a known state, or the move
            from the current state to it is not allowed.
    """
    if to_state not in STATES:                                     # guard against a typo'd target
        raise IllegalStateMove(f"{to_state!r} is not a known request state")
    current = getattr(record, "state", None) or INITIAL_STATE      # a missing state reads as the initial one
    if to_state not in ALLOWED_MOVES.get(current, set()):          # the move itself must be allowed
        extra = f" ({reason})" if reason else ""
        raise IllegalStateMove(f"cannot move request state from {current!r} to {to_state!r}{extra}")
    record.state = to_state                                        # apply the move; the session commits it
    return to_state


def backfill_state(status: str, *, has_submit_event: bool, legacy: bool) -> str:
    """The state a row should hold, derived from its status and a few signals.

    The single rule the migration backfill and the importer share, so a row gets
    the same state whether it is backfilled once or re-imported. A decided status
    maps straight across. A pending row is ``waiting_on_approval`` when it was
    really put into the approval flow (it was submitted/imported, or is a legacy
    row from before this app), regardless of whether a manager is set: submit
    makes a request decidable the moment the employee resolves, even with no
    manager, matching ``request_submission._do_submit`` (the admin cover list then
    carries a no-manager request). It is ``needs_attention`` otherwise (a pending
    row the old pipeline never finished).

    Args:
        status: The row's lifecycle status (lower case).
        has_submit_event: Whether a submitted / imported / sent_to_manager event
            exists for the row (it was really put into the approval flow).
        legacy: Whether the row predates this app (decided the old way).

    Returns:
        One of ``request_vocab.STATES``.
    """
    mapped = STATUS_TO_STATE.get(status)                           # approved/rejected/refunded/cancelled
    if mapped is not None:
        return mapped
    if status == "pending" and (legacy or has_submit_event):
        return "waiting_on_approval"                               # ready to be decided, manager or not
    return "needs_attention" if status == "pending" else INITIAL_STATE   # unfinished pending, else fresh


# How long a getting_ready row may sit before the startup reconcile assumes its
# submit crashed and finishes the move submit should have made (#455). Matches the
# admin stuck list's getting_ready window.
GETTING_READY_RECONCILE_MINUTES = 10

# The request-event types that mean a request was really put into the approval
# flow, so a getting_ready row carrying one should have reached waiting_on_approval.
SUBMIT_EVENT_TYPES = ("submitted", "imported", "sent_to_manager")


def reconcile_decided_state(status: str, state: str) -> str | None:
    """The corrected state for a decided row whose state disagrees with its status.

    Pure so it tests without a database. A decided status (approved / rejected /
    refunded / cancelled) implies exactly one state; when the stored state is not
    that, this returns the state the status implies, else None (leave it alone). A
    pending row is never touched here (its drift is the getting_ready rule below).

    Args:
        status: The row's lifecycle status (lower case).
        state: The row's current state-machine position.

    Returns:
        The state the status implies when it disagrees, else None.
    """
    mapped = STATUS_TO_STATE.get(status)                           # a decided status -> its one state
    if mapped is not None and state != mapped:
        return mapped                                              # status wins: state was left behind
    return None                                                    # agree, or a pending row: no change


async def reconcile_request_states() -> dict:
    """Fix request-state drift left by a deploy overlapping an in-flight decision.

    Idempotent and Postgres-only; it never raises, so a failure cannot stall
    startup. Two corrections, both derived from data already on the row:

    - a decided row (approved / rejected / refunded / cancelled) whose ``state``
      was left behind is set to the state the status implies;
    - a ``getting_ready`` row older than ``GETTING_READY_RECONCILE_MINUTES`` that
      carries a submit/import/sent_to_manager event is moved to
      ``waiting_on_approval`` (its submit committed the row but a restart cut in
      before the state move).

    Returns:
        ``{"decided_fixed", "getting_ready_fixed"}`` counts (0/0 when off Postgres
        or when nothing drifted); the same counts are logged.
    """
    from datetime import timedelta

    from sqlalchemy import select

    try:
        from app.database import async_session
        from app.models.mixins import utcnow
        from app.models.request_event import RequestEvent
        from app.models.request_record import RequestRecord
        from app.repositories.request_store import requests_in_postgres

        if not await requests_in_postgres():
            return {"decided_fixed": 0, "getting_ready_fixed": 0}   # SharePoint path has no state to reconcile

        decided_fixed = 0
        getting_ready_fixed = 0
        cutoff = utcnow() - timedelta(minutes=GETTING_READY_RECONCILE_MINUTES)
        async with async_session() as session:
            rows = (await session.execute(select(RequestRecord))).scalars().all()
            for record in rows:
                corrected = reconcile_decided_state(record.status, record.state)
                if corrected is not None:
                    record.state = corrected                        # status wins; write the implied state
                    decided_fixed += 1
                    continue
                if record.state == "getting_ready" and record.created_at is not None:
                    created = record.created_at
                    if created.tzinfo is None:                      # SQLite hands back naive UTC
                        from datetime import timezone
                        created = created.replace(tzinfo=timezone.utc)
                    if created <= cutoff:                           # old enough that submit should have finished
                        asked = (await session.execute(
                            select(RequestEvent.id).where(
                                RequestEvent.request_id == record.id,
                                RequestEvent.type.in_(SUBMIT_EVENT_TYPES),
                            ).limit(1)
                        )).first()
                        if asked is not None:                       # it was submitted: finish the move
                            move(record, "waiting_on_approval", reason="startup reconcile")
                            getting_ready_fixed += 1
            await session.commit()
        logger.info(
            "Request-state reconcile: %d decided rows realigned, %d getting_ready rows advanced",
            decided_fixed, getting_ready_fixed,
        )
        return {"decided_fixed": decided_fixed, "getting_ready_fixed": getting_ready_fixed}
    except Exception:                                               # never let a reconcile break startup
        logger.exception("Request-state reconcile failed")
        return {"decided_fixed": 0, "getting_ready_fixed": 0}
