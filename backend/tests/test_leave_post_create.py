"""A failed post-create step on a new leave request is surfaced, not swallowed (finding 11).

Creating a leave request kicks off its day calculation, manager assignment and
bereavement alert. Those run under ``gather(return_exceptions=True)``, which
keeps one failure from cancelling the others but also hides it: in Postgres mode
the manager assignment is the ``submit`` that queues the approval email and
events, so a swallowed failure leaves a committed request with no manager and
nobody told, while the route still reports success. These pin that each failure
is logged and flagged on the request instead.
"""

import asyncio
import itertools

import pytest
from datetime import date
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services import leave_requests as lr

_ids = itertools.count(40_000_000)


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture
def _clean():
    """Wipe the request and event rows these tests insert."""
    async def wipe():
        async with async_session() as session:
            await session.execute(delete(BalanceLedger))
            await session.execute(delete(RequestEvent))
            await session.execute(delete(RequestRecord))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


def test_a_post_create_failure_is_logged_and_flagged_not_swallowed(monkeypatch):
    # The manager assignment raises; the request is still created and returned,
    # and the failure is routed to _flag_post_create_failure rather than lost.
    flagged = []

    async def fake_create(list_id, fields):
        return {"id": "501", "fields": fields}

    async def ok(*_a, **_k):
        return None

    async def boom(item_id, notify=True):
        raise RuntimeError("submit blew up")

    async def no_lookup(_email):
        return None

    async def flag(item_id, exc):
        flagged.append((str(item_id), exc))

    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")   # skip the Postgres EmployeeID lookup branch
    monkeypatch.setattr(lr.request_store, "create_list_item", fake_create)
    monkeypatch.setattr(lr, "auto_calculate_days", ok)
    monkeypatch.setattr(lr, "send_bereavement_alert", ok)
    monkeypatch.setattr(lr, "auto_assign_manager", boom)
    monkeypatch.setattr(lr, "_flag_post_create_failure", flag)
    monkeypatch.setattr(lr, "_resolve_user_lookup_id", no_lookup)

    form = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03",
            "first_name": "Pat", "last_name": "Staff"}
    item = run(lr.process_new_leave_request(form, "pat@ucsh.com", "Request page"))

    assert item["id"] == "501"                                   # the request was still created
    assert len(flagged) == 1                                     # the failure was surfaced, not swallowed
    assert flagged[0][0] == "501" and isinstance(flagged[0][1], RuntimeError)


def test_flag_post_create_failure_marks_the_request_in_postgres(monkeypatch, _clean):
    # In Postgres mode the flag is a real "flagged" timeline event, from the
    # "system" channel, carrying the error, so the stuck views can show it.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    item_id = next(_ids)

    async def seed():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="leave", item_id=item_id, status="pending", employee_id="7",
                employee_name="Pat Staff", leave_type="Vacation",
                start_date=date(2026, 11, 2), approval_version=1, balance_state="not_applied",
            ))
            await session.commit()
    run(seed())

    run(lr._flag_post_create_failure(item_id, RuntimeError("submit blew up")))

    async def events():
        async with async_session() as session:
            return (await session.execute(
                select(RequestEvent).where(RequestEvent.item_id == item_id, RequestEvent.kind == "leave")
            )).scalars().all()

    flagged = [e for e in run(events()) if e.type == "flagged"]
    assert len(flagged) == 1
    assert flagged[0].channel == "system"
    assert "RuntimeError" in (flagged[0].detail or {}).get("error", "")


def test_flag_post_create_failure_is_a_no_op_on_sharepoint(monkeypatch, _clean):
    # SharePoint keeps no event log, so flagging there writes nothing (the logged
    # exception is the only signal); it must not raise.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    item_id = next(_ids)

    run(lr._flag_post_create_failure(item_id, RuntimeError("submit blew up")))

    async def events():
        async with async_session() as session:
            return (await session.execute(
                select(RequestEvent).where(RequestEvent.item_id == item_id)
            )).scalars().all()

    assert run(events()) == []
