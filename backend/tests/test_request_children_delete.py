"""Deleting a typed request on Postgres leaves the right children (finding 4).

The admin delete goes through ``TypedRequestList.delete_list_item``. Deleting a
request must take its timeline and field history with it (ON DELETE CASCADE) but
keep its balance history and the record that a message was sent (ON DELETE SET
NULL), so a payroll dispute or a "did they get the email?" question is still
answerable after the request is gone.

SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys=ON`` is set per
connection, and the production database is Postgres where they are always on. So
this test turns the pragma on with a connect-time listener (added and removed
here, so other tests are unaffected) and proves the ON DELETE rules with real
foreign keys. In production Alembic 0021 sets the same rules on Postgres.
"""
import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, event, select

from app.database import async_session, engine
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture
def foreign_keys_on():
    """Enforce SQLite foreign keys for this test only, then restore the default.

    Adds a connect-time listener that runs ``PRAGMA foreign_keys=ON`` on each new
    connection, and disposes the pool so connections already open (which would
    not have the pragma) are dropped and reopened with it. Both are undone after
    the test so the rest of the suite keeps SQLite's default (foreign keys off).
    """
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")               # enforce the ON DELETE rules
        cursor.close()

    event.listen(engine.sync_engine, "connect", _fk_on)        # every new connection gets it
    run(engine.dispose())                                      # drop pooled connections without the pragma
    yield
    event.remove(engine.sync_engine, "connect", _fk_on)
    run(engine.dispose())                                      # later tests get plain connections again


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the request tables before and after each test."""
    async def wipe():
        async with async_session() as session:
            for model in (BalanceLedger, Outbox, RequestEvent, RequestChange,
                          RequestRecord, RequestCounter):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _seed_request_with_children() -> tuple[int, int, int, int]:
    """Insert one request plus an event, a change, a ledger row and an outbox row.

    Returns:
        ``(item_id, event_id, ledger_id, outbox_id)`` for reading them back.
    """
    async with async_session() as session:
        request = RequestRecord(
            kind="leave", item_id=42, status="approved", employee_id="7",
            employee_name="Pat", leave_type="Sick or Personal Day",
            start_date=date(2026, 11, 2), approval_version=1, balance_state="applied",
        )
        session.add(request)
        await session.flush()                                  # assign request.id
        event_row = RequestEvent(
            request_id=request.id, kind="leave", item_id=42, employee_id="7",
            type="approved", channel="email_link", detail={}, idempotency_key="leave:42:approved:v1",
        )
        change = RequestChange(request_id=request.id, field="Days", old_value=1, new_value=2)
        ledger = BalanceLedger(
            request_id=request.id, employee_id="7", pot="sick", change=-2.0,
            before=5.0, after=3.0, channel="email_link", idempotency_key="leave:42:ledger:sick:v1",
        )
        session.add_all([event_row, change, ledger])
        await session.flush()
        outbox = Outbox(
            request_id=request.id, event_id=event_row.id, message_type="result",
            channel="email", recipient="pat@ucsh.com", body="approved",
            status="sent", attempts=1, idempotency_key="leave:42:result:v1",
        )
        session.add(outbox)
        await session.commit()
        return request.item_id, event_row.id, ledger.id, outbox.id


def test_deleting_a_request_cascades_the_timeline_and_keeps_the_ledger(foreign_keys_on):
    item_id, event_id, ledger_id, outbox_id = run(_seed_request_with_children())

    # The admin delete path: remove the request through the typed adapter.
    run(TypedRequestList("leave").delete_list_item("leave-list", item_id))

    async def read():
        async with async_session() as session:
            events = (await session.execute(select(RequestEvent))).scalars().all()
            changes = (await session.execute(select(RequestChange))).scalars().all()
            ledger = await session.get(BalanceLedger, ledger_id)
            outbox = await session.get(Outbox, outbox_id)
            return events, changes, ledger, outbox

    events, changes, ledger, outbox = run(read())
    # Timeline and field history go with the request (CASCADE).
    assert events == [] and changes == []
    # Balance history survives, with the request link cleared (SET NULL).
    assert ledger is not None and ledger.request_id is None and ledger.after == 3.0
    # The sent-message record survives, with request and event links cleared (SET NULL).
    assert outbox is not None and outbox.request_id is None and outbox.event_id is None
