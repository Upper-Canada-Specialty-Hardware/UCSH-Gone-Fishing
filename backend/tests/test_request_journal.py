"""The shared write helpers: one step lands at most once, and the stage follows.

append_event, post_ledger and enqueue_message each insert behind a savepoint, so
a repeated idempotency key is a no-op that leaves the outer transaction usable
and the other writes of the same decision still commit. append_event also moves
the request's stage forward, and record_change writes a field-history row.
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services import request_journal as rj


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def clean():
    """Empty the typed request tables before and after each test."""

    async def wipe():
        async with async_session() as session:
            # Children before parents, so foreign keys never block a delete.
            for model in (Outbox, BalanceLedger, RequestEvent, RequestChange, RequestRecord, RequestCounter):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _make_request(session) -> RequestRecord:
    """Store one valid pending leave request and return it (already flushed)."""
    request = RequestRecord(
        kind="leave", item_id=3905, employee_id="7", manager_id="3",
        leave_type="Vacation", start_date=date(2026, 11, 2), status="pending",
    )
    session.add(request)
    await session.flush()                                       # assign id so the helpers can reference it
    return request


def test_append_event_is_idempotent_and_the_transaction_stays_usable(clean):
    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            key = rj.step_key("leave", request.item_id, "approved", 1)
            first = await rj.append_event(session, request, "approved", "email_link", "Dave", {"x": 1}, key)
            second = await rj.append_event(session, request, "approved", "email_link", "Dave", {"x": 1}, key)
            # The duplicate returned None; the outer transaction must still accept writes.
            later = await rj.append_event(
                session, request, "result_sent", "system", None, {}, rj.step_key("leave", request.item_id, "result_sent", 1)
            )
            await session.commit()
            return first, second, later, request.stage
        return None

    first, second, later, stage = run(go())
    assert first is not None and second is None and later is not None
    assert stage == rj.STAGE_WORDS["result_sent"]               # stage followed the last event, not the duplicate

    async def count_events():
        async with async_session() as session:
            return int((await session.execute(select(func.count()).select_from(RequestEvent))).scalar_one())

    assert run(count_events()) == 2                             # the duplicate never inserted


def test_the_ledger_is_idempotent(clean):
    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            key = rj.step_key("leave", request.item_id, "ledger:sick", 1)
            first = await rj.post_ledger(session, request, "7", "sick", 5, 3, "spent", "Dave", "email_link", key)
            second = await rj.post_ledger(session, request, "7", "sick", 5, 3, "spent", "Dave", "email_link", key)
            await session.commit()
            return first, second

    first, second = run(go())
    assert first is not None and second is None
    assert first.change == -2                                   # after - before = 3 - 5


def test_a_bad_row_is_an_error_not_a_duplicate(clean):
    """A ledger row that breaks a CHECK must raise, never pass as already recorded."""
    from sqlalchemy.exc import IntegrityError

    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            key = rj.step_key("leave", request.item_id, "ledger:nowhere", 1)
            await rj.post_ledger(session, request, "7", "not_a_pot", 5, 3, "spent", "Dave", "email_link", key)

    with pytest.raises(IntegrityError):
        run(go())


def test_the_outbox_is_idempotent(clean):
    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            key = "leave:3905:result_sent:v1:email:pat@ucsh.com"
            first = await rj.enqueue_message(
                session, request, None, "result", "email", "pat@ucsh.com", "Subject", "<b>body</b>", key
            )
            second = await rj.enqueue_message(
                session, request, None, "result", "email", "pat@ucsh.com", "Subject", "<b>body</b>", key
            )
            await session.commit()
            return first, second

    first, second = run(go())
    assert first is not None and second is None


def test_append_event_sets_the_stage(clean):
    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            await rj.append_event(
                session, request, "sent_to_manager", "system", None, {}, rj.step_key("leave", request.item_id, "sent_to_manager", 1)
            )
            await session.commit()
            return request.stage

    assert run(go()) == rj.STAGE_WORDS["sent_to_manager"]


def test_record_change_writes_a_row(clean):
    async def go():
        async with async_session() as session:
            request = await _make_request(session)
            rj.record_change(session, request, "Status", "Pending", "Approved", "Dave", "email_link", "manager approved")
            await session.commit()
        async with async_session() as session:
            return (await session.execute(select(RequestChange))).scalars().all()

    rows = run(go())
    assert len(rows) == 1 and rows[0].field == "Status"
    assert rows[0].old_value == "Pending" and rows[0].new_value == "Approved"


def test_next_item_id_increments_per_kind(clean):
    async def go():
        async with async_session() as session:
            a = await rj.next_item_id(session, "leave")
            b = await rj.next_item_id(session, "leave")
            c = await rj.next_item_id(session, "overtime")
            await session.commit()
            return a, b, c

    assert run(go()) == (1, 2, 1)                               # each kind counts on its own, from 1
