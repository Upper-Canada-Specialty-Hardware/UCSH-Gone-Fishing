"""The write helpers every approval step shares, so a step happens at most once.

A single request decision touches several tables: the request's status, the
balance pots it moves, the timeline event, the message it queues. These helpers
write each of those, but none of them commits: the caller opens one session,
calls the helpers it needs, and commits once, so the whole decision lands
together or not at all.

Each write carries an idempotency key, built with ``step_key``. Inserting a key
that already exists is a no-op: the insert runs inside a SAVEPOINT, and a
unique-key clash rolls back only that savepoint and returns ``None``, leaving
the outer transaction alive so the caller's other writes still commit. That is
how the same step arriving from the email link, an SMS reply and the dashboard
at once still lands exactly once (#321-#327).
"""
import logging

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord

logger = logging.getLogger(__name__)

# The stage line to show for each event type, kept short and in plain words.
# append_event copies the matching line onto requests.stage so the stage shown
# on a dashboard always matches the latest event.
STAGE_WORDS = {
    "submitted": "Submitted",
    "sent_to_manager": "Waiting on manager",
    "receipt_sent": "Submitted, receipt sent",
    "link_opened": "Manager opened the link",
    "approved": "Approved",
    "rejected": "Rejected",
    "refused": "Action was refused",
    "balance_applied": "Approved, balance updated",
    "counted": "Approved, vacation counted",
    "no_balance_used": "Approved, no balance used",
    "result_sent": "Result sent",
    "send_failed": "A message failed to send",
    "reminder_sent": "Reminder sent to manager",
    "refunded": "Refunded",
    "balance_reversed": "Balance reversed",
    "edited": "Edited by an admin",
    "cancelled": "Cancelled",
    "imported": "Imported from SharePoint",
    "flagged": "Flagged for review",
}

# The NOTIFY channel a live-events reader listens on; Postgres delivers at commit.
NOTIFY_CHANNEL = "request_events"


def step_key(kind: str, item_id: int, step: str, version: int, suffix: str | None = None) -> str:
    """Build the idempotency key for one step of one request.

    The shape is ``{kind}:{item_id}:{step}:v{version}`` with an optional
    ``:{suffix}`` for a repeatable step (a reminder's date) . The step may itself
    carry a sub-part, e.g. ``"ledger:sick"``, giving ``leave:3905:ledger:sick:v1``.

    Args:
        kind: The request kind.
        item_id: The request's item_id (the number people see).
        step: The event type, or a named sub-step like ``"ledger:sick"``.
        version: The request's approval_version at the time.
        suffix: A date or sequence that makes a repeatable step unique, or None.

    Returns:
        The idempotency key string.
    """
    base = f"{kind}:{item_id}:{step}:v{version}"                 # the one-time-per-version key
    return f"{base}:{suffix}" if suffix is not None else base    # add the suffix for repeatable steps


async def next_item_id(session: AsyncSession, kind: str) -> int:
    """Hand out the next item_id for a kind, bumping its counter atomically.

    Args:
        session: An open session; not committed here.
        kind: The request kind the id is for.

    Returns:
        The new item_id (the counter's value after the bump).
    """
    # Bump an existing counter and read the new value in one statement.
    result = await session.execute(
        update(RequestCounter)
        .where(RequestCounter.kind == kind)
        .values(last_item_id=RequestCounter.last_item_id + 1)
        .returning(RequestCounter.last_item_id)
    )
    row = result.first()
    if row is not None:                                         # the counter existed and was bumped
        return int(row[0])
    # No counter yet: create it at 1, race-safe behind a savepoint.
    try:
        async with session.begin_nested():
            session.add(RequestCounter(kind=kind, last_item_id=1))
        return 1
    except IntegrityError:
        # Another worker created it first; bump the now-existing row instead.
        result = await session.execute(
            update(RequestCounter)
            .where(RequestCounter.kind == kind)
            .values(last_item_id=RequestCounter.last_item_id + 1)
            .returning(RequestCounter.last_item_id)
        )
        return int(result.first()[0])


async def _insert_or_skip(session: AsyncSession, row) -> bool:
    """Insert a row behind a savepoint; return False if its key already existed.

    The savepoint means a unique-key clash rolls back only this insert, never
    the caller's outer transaction, so the other writes of the same decision
    still commit.

    Args:
        session: An open session; not committed here.
        row: The ORM row to insert (it carries the unique idempotency key).

    Returns:
        True when the row was inserted, False when its key was already present.
    """
    try:
        async with session.begin_nested():                     # a SAVEPOINT around just this insert
            session.add(row)
            await session.flush()                              # force the INSERT so a clash raises now
        return True
    except IntegrityError:
        # Only a clash on the idempotency key is a no-op; any other failure
        # (a CHECK or a foreign key) is a real error and must not vanish.
        model = type(row)
        existing = await session.execute(
            select(model.id).where(model.idempotency_key == row.idempotency_key)
        )
        if existing.first() is None:
            raise                                              # not a duplicate: surface the real error
        return False                                           # the key already existed: a no-op


async def append_event(
    session: AsyncSession,
    request: RequestRecord,
    type: str,
    channel: str,
    actor: str | None = None,
    detail: dict | None = None,
    key: str | None = None,
) -> RequestEvent | None:
    """Add one timeline event, at most once per key, and advance the stage.

    On insert it also sets ``request.stage`` to the plain-words line for the
    event type, so the stage stays in step with the timeline, and on Postgres it
    fires ``pg_notify`` so a live reader wakes at commit.

    Args:
        session: An open session; not committed here.
        request: The request the event is about.
        type: The event type (one of request_vocab.EVENT_TYPES).
        channel: Where the event came from.
        actor: Who caused it, or None.
        detail: What a timeline line needs, or None for an empty object.
        key: The idempotency key; the event records at most once per key.

    Returns:
        The inserted RequestEvent, or None when the key already existed.
    """
    event = RequestEvent(
        request_id=request.id,
        kind=request.kind,
        item_id=request.item_id,
        employee_id=request.employee_id,
        manager_id=request.manager_id,
        type=type,
        channel=channel,
        actor=actor,
        detail=detail or {},
        idempotency_key=key,
    )
    if not await _insert_or_skip(session, event):
        return None                                            # already recorded: leave the stage as it is
    stage = STAGE_WORDS.get(type)
    if stage:
        request.stage = stage                                  # keep the stage line in step with the event
    if session.bind.dialect.name == "postgresql":
        # Tell any live reader a new event landed; Postgres delivers at commit.
        await session.execute(
            text(f"SELECT pg_notify('{NOTIFY_CHANNEL}', :payload)"), {"payload": str(event.id)}
        )
    return event


async def post_ledger(
    session: AsyncSession,
    request: RequestRecord | None,
    employee_id: str,
    pot: str,
    before: float,
    after: float,
    reason: str | None,
    actor: str | None,
    channel: str,
    key: str,
) -> BalanceLedger | None:
    """Record one pot movement, at most once per key.

    Args:
        session: An open session; not committed here.
        request: The request the movement belongs to, or None for a hand edit.
        employee_id: Whose balance moved.
        pot: Which pot (one of request_vocab.POTS).
        before: The pot value before the movement.
        after: The pot value after the movement.
        reason: Why, in one plain sentence, or None.
        actor: Who caused it, or None.
        channel: Where it came from.
        key: The idempotency key.

    Returns:
        The inserted BalanceLedger, or None when the key already existed.
    """
    row = BalanceLedger(
        request_id=request.id if request is not None else None,
        employee_id=employee_id,
        pot=pot,
        change=after - before,                                 # the signed amount moved
        before=before,
        after=after,
        reason=reason,
        actor=actor,
        channel=channel,
        idempotency_key=key,
    )
    return row if await _insert_or_skip(session, row) else None


async def enqueue_message(
    session: AsyncSession,
    request: RequestRecord | None,
    event: RequestEvent | None,
    message_type: str,
    channel: str,
    recipient: str,
    subject: str | None,
    body: str,
    key: str,
) -> Outbox | None:
    """Queue one message to send, at most once per key.

    Args:
        session: An open session; not committed here.
        request: The request the message is for, or None.
        event: The event that calls for it, or None.
        message_type: The kind of message (manager_approval, receipt, ...).
        channel: "email" or "sms".
        recipient: The address or phone number.
        subject: The email subject, or None for an SMS.
        body: The rendered message; stored, never returned by an API.
        key: The idempotency key, usually ``{event key}:{channel}:{recipient}``.

    Returns:
        The inserted Outbox row, or None when the key already existed.
    """
    row = Outbox(
        request_id=request.id if request is not None else None,
        event_id=event.id if event is not None else None,
        message_type=message_type,
        channel=channel,
        recipient=recipient,
        subject=subject,
        body=body,
        status="pending",
        attempts=0,
        idempotency_key=key,
    )
    return row if await _insert_or_skip(session, row) else None


def record_change(
    session: AsyncSession,
    request: RequestRecord,
    field: str,
    old,
    new,
    actor: str | None = "system",
    channel: str | None = "system",
    reason: str | None = None,
) -> RequestChange:
    """Append one field-change row for a request (not keyed: always inserted).

    Args:
        session: An open session; the row is added, not committed.
        request: The request whose field changed (must already have an id).
        field: The SharePoint column name that changed.
        old: The value before, JSON-serialisable.
        new: The value after, JSON-serialisable.
        actor: Who changed it.
        channel: Where the change came from.
        reason: Why, when there is a reason.

    Returns:
        The RequestChange added to the session.
    """
    change = RequestChange(
        request_id=request.id,
        field=field,
        old_value=old,
        new_value=new,
        actor=actor,
        channel=channel,
        reason=reason,
    )
    session.add(change)                                        # flushed with the caller's other writes
    return change


def log_step(request: RequestRecord, step: str, **detail) -> None:
    """Write one structured log line for a step, keyed by the request (#327).

    Args:
        request: The request the step is on.
        step: The event type or named sub-step.
        **detail: Extra key/value pairs to include in the line.
    """
    extra = " ".join(f"{name}={value}" for name, value in detail.items())   # k=v pairs for the line
    logger.info("request %s:%s %s %s", request.kind, request.item_id, step, extra)
