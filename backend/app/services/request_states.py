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
from app.models.request_vocab import STATES

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


def backfill_state(status: str, *, has_manager: bool, has_submit_event: bool, legacy: bool) -> str:
    """The state a row should hold, derived from its status and a few signals.

    The single rule the migration backfill and the importer share, so a row gets
    the same state whether it is backfilled once or re-imported. A decided status
    maps straight across. A pending row is ``waiting_on_approval`` when it is
    ready to be decided (it has a manager and was actually submitted/imported, or
    it is a legacy row from before this app), and ``needs_attention`` otherwise (a
    pending row the old pipeline never finished).

    Args:
        status: The row's lifecycle status (lower case).
        has_manager: Whether the row has a manager assigned.
        has_submit_event: Whether a submitted / imported / sent_to_manager event
            exists for the row (it was really put into the approval flow).
        legacy: Whether the row predates this app (decided the old way).

    Returns:
        One of ``request_vocab.STATES``.
    """
    mapped = STATUS_TO_STATE.get(status)                           # approved/rejected/refunded/cancelled
    if mapped is not None:
        return mapped
    if status == "pending" and (legacy or (has_manager and has_submit_event)):
        return "waiting_on_approval"                               # ready to be decided
    return "needs_attention" if status == "pending" else INITIAL_STATE   # unfinished pending, else fresh
