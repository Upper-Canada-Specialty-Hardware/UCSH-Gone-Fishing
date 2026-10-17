"""The outbox sender and worker: deliver once, retry with backoff, drop in rehearsal (#326).

A decision only queues a message; these cover what actually sends it. The email
and SMS send functions are stubbed, so nothing leaves the test. The checks are:
a success marks the row sent and logs a result_sent event; a provider error marks
it failed with a growing backoff; a claimed/sent row is never sent twice; and a
rehearsal recipient off the allowlist is dropped unsent.
"""
import asyncio
import itertools
import sys
import types
from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.mixins import utcnow
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services.outbox_sender import _backoff_seconds, send_outbox_row
from app.tasks import outbox_worker

_keys = itertools.count(1)
_item_ids = itertools.count(70_000_000)


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the outbox and request tables before and after each test."""
    async def wipe():
        async with async_session() as session:
            for model in (Outbox, RequestEvent, RequestChange, RequestRecord, RequestCounter):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _request() -> RequestRecord:
    """Insert one approved leave request to tie events to, and return it."""
    async with async_session() as session:
        record = RequestRecord(
            kind="leave", item_id=next(_item_ids), status="approved",
            employee_id="7", employee_email="pat@ucsh.com", employee_name="Pat",
            leave_type="Sick or Personal Day", start_date=date(2026, 11, 2),
            approval_version=1, balance_state="applied",
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


async def _enqueue(request, channel="email", recipient="pat@ucsh.com", status="sending") -> int:
    """Insert one outbox row and return its id.

    Args:
        request: The request to tie it to, or None.
        channel: "email" or "sms".
        recipient: The address or phone number.
        status: The starting status (the worker marks a row "sending" before send).

    Returns:
        The new row's id.
    """
    async with async_session() as session:
        row = Outbox(
            request_id=request.id if request else None,
            message_type="result", channel=channel, recipient=recipient,
            subject="Leave Request: Approved" if channel == "email" else None,
            body="<p>your request was approved</p>" if channel == "email" else "approved",
            status=status, attempts=0, idempotency_key=f"key-{next(_keys)}",
        )
        session.add(row)
        await session.commit()
        return row.id


def _stub_email(monkeypatch, result=None, error=None):
    """Replace app.graph.email.send_email with a stub, and count its calls.

    Args:
        monkeypatch: The pytest fixture.
        result: An object to return (its email_id becomes provider_message_id).
        error: An exception to raise instead of returning.

    Returns:
        A list that gets one entry per call, for asserting the count.
    """
    calls = []

    async def fake_send(recipients, subject, body):
        calls.append((tuple(recipients), subject))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr("app.graph.email.send_email", fake_send)
    return calls


# ----- a single row: success and failure -----


def test_a_successful_send_marks_the_row_sent_and_logs_a_result_event(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="MSG-1"))
    request = run(_request())
    row_id = run(_enqueue(request))

    async def deliver():
        async with async_session() as session:
            row = await session.get(Outbox, row_id)
            await send_outbox_row(session, row)
            await session.commit()

    run(deliver())

    async def read():
        async with async_session() as session:
            row = await session.get(Outbox, row_id)
            events = (await session.execute(
                select(RequestEvent).where(RequestEvent.request_id == request.id)
            )).scalars().all()
            return row, events

    row, events = run(read())
    assert len(calls) == 1
    assert row.status == "sent" and row.attempts == 1 and row.sent_at is not None
    assert row.provider_message_id == "MSG-1" and row.last_error is None
    assert any(event.type == "result_sent" for event in events)


def test_a_failed_send_sets_failed_with_a_backoff_and_a_send_failed_event(monkeypatch):
    _stub_email(monkeypatch, error=RuntimeError("provider down"))
    request = run(_request())
    row_id = run(_enqueue(request))

    async def deliver():
        async with async_session() as session:
            row = await session.get(Outbox, row_id)
            await send_outbox_row(session, row)
            await session.commit()

    run(deliver())

    async def read():
        async with async_session() as session:
            row = await session.get(Outbox, row_id)
            events = (await session.execute(
                select(RequestEvent).where(RequestEvent.request_id == request.id)
            )).scalars().all()
            return row, events

    row, events = run(read())
    assert row.status == "failed" and row.attempts == 1
    assert row.next_attempt_at is not None and "provider down" in row.last_error
    assert any(event.type == "send_failed" for event in events)


def test_the_backoff_doubles_and_is_capped_at_an_hour():
    assert _backoff_seconds(1) == 60                              # first retry: one minute
    assert _backoff_seconds(2) == 120                             # then double
    assert _backoff_seconds(3) == 240
    assert _backoff_seconds(20) == 3600                           # capped at an hour


# ----- rehearsal mode drops a disallowed recipient -----


def test_rehearsal_drops_a_recipient_not_on_the_allowlist(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="MSG-2"))
    # Stand in a rehearsal module the sync work owns: this recipient is not allowed.
    fake_module = types.ModuleType("app.services.rehearsal")
    fake_module.allow_recipient = lambda recipient: False
    monkeypatch.setitem(sys.modules, "app.services.rehearsal", fake_module)
    request = run(_request())
    row_id = run(_enqueue(request))

    async def deliver():
        async with async_session() as session:
            row = await session.get(Outbox, row_id)
            await send_outbox_row(session, row)
            await session.commit()

    run(deliver())

    async def read():
        async with async_session() as session:
            return await session.get(Outbox, row_id)

    row = run(read())
    assert row.status == "dropped" and calls == []               # nothing sent
    assert "rehearsal" in row.last_error


# ----- the worker drains due rows exactly once -----


def test_the_worker_drains_due_rows_and_never_sends_one_twice(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="MSG-3"))
    request = run(_request())
    # Two due rows, left "pending" so the worker claims them.
    run(_enqueue(request, status="pending"))
    run(_enqueue(request, status="pending"))

    run(outbox_worker._drain())                                  # first pass: claim and send both
    assert len(calls) == 2

    run(outbox_worker._drain())                                  # second pass: nothing left to send
    assert len(calls) == 2                                       # no double send

    async def statuses():
        async with async_session() as session:
            rows = (await session.execute(select(Outbox))).scalars().all()
            return [row.status for row in rows]

    assert run(statuses()) == ["sent", "sent"]


# ----- a stalled "sending" claim is reclaimed (finding 6) -----


async def _enqueue_sending(request, *, claimed_minutes_ago: float | None) -> int:
    """Insert one "sending" outbox row with a chosen claim age, and return its id.

    Args:
        request: The request to tie it to, or None.
        claimed_minutes_ago: How long ago it was claimed, or None for no stamp
            (a row claimed before the claimed_at column existed).

    Returns:
        The new row's id.
    """
    claimed_at = None if claimed_minutes_ago is None else utcnow() - timedelta(minutes=claimed_minutes_ago)
    async with async_session() as session:
        row = Outbox(
            request_id=request.id if request else None,
            message_type="result", channel="email", recipient="pat@ucsh.com",
            subject="Leave Request: Approved", body="<p>approved</p>",
            status="sending", attempts=0, claimed_at=claimed_at,
            idempotency_key=f"key-{next(_keys)}",
        )
        session.add(row)
        await session.commit()
        return row.id


def test_a_stale_sending_row_is_reclaimed_and_a_fresh_one_is_left():
    request = run(_request())
    stale_id = run(_enqueue_sending(request, claimed_minutes_ago=20))      # past the 10-minute timeout
    null_id = run(_enqueue_sending(request, claimed_minutes_ago=None))     # pre-column claim, treated as stale
    fresh_id = run(_enqueue_sending(request, claimed_minutes_ago=1))       # still in flight, leave it

    claimed = run(outbox_worker._claim_due(10))                            # reclaims stale, then claims due

    async def read(row_id):
        async with async_session() as session:
            return await session.get(Outbox, row_id)

    # The two abandoned claims are freed and re-claimed with a fresh stamp.
    assert stale_id in claimed and null_id in claimed
    reclaimed = run(read(stale_id))
    assert reclaimed.status == "sending" and reclaimed.claimed_at is not None
    # The fresh claim is left alone: still "sending", not re-claimed this pass.
    assert fresh_id not in claimed
    assert run(read(fresh_id)).status == "sending"
