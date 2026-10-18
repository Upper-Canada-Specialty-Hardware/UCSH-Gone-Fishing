"""Staff write-backs for one SharePoint item are drained in id order (finding 8).

Each write-back replays an absolute set of field values, so two queued for the
same item must land oldest first or a stale value can overwrite a newer one. The
claim serializes per item: only the head (lowest id) of each item is claimed in
a pass, and a head that is not yet due (backing off, or an unexpired claim) holds
its item's whole queue. Different items stay independent.
"""

import asyncio
import pytest
from datetime import timedelta

from sqlalchemy import delete

from app.database import async_session
from app.models.mixins import utcnow
from app.models.staff import StaffWriteback
from app.services import staff_writeback as wb


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Empty the write-back queue before and after each test."""
    async def wipe():
        async with async_session() as session:
            await session.execute(delete(StaffWriteback))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


async def _enqueue(sp_item_id, *, status="pending", next_attempt_at=None) -> int:
    """Insert one write-back row and return its id (ids rise with insert order)."""
    async with async_session() as session:
        row = StaffWriteback(
            sp_item_id=str(sp_item_id), fields={"CurrentVacationBalance": 1},
            op="update", status=status, attempts=0, next_attempt_at=next_attempt_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _claim(limit=10) -> list[int]:
    """Run one claim pass and return the ids claimed, in order."""
    async with async_session() as session:
        rows = await wb._claim_due_rows(session, utcnow(), limit)
        return [r.id for r in rows]


def test_only_the_oldest_row_per_item_is_claimed_in_a_pass():
    # Two write-backs for the same item: only the head (lower id) is claimed; the
    # newer one waits, so the absolute replays land oldest first.
    first = run(_enqueue("500"))
    second = run(_enqueue("500"))

    assert run(_claim()) == [first]                            # the newer row is held behind the head

    # Finish the head, and the next pass claims the one that was waiting.
    async def finish(row_id):
        async with async_session() as session:
            row = await session.get(StaffWriteback, row_id)
            row.status = wb.DONE
            row.next_attempt_at = None
            await session.commit()
    run(finish(first))

    assert run(_claim()) == [second]


def test_rows_for_different_items_are_all_claimed():
    # Different items are independent, so both heads are claimed in one pass.
    a = run(_enqueue("500"))
    b = run(_enqueue("600"))

    assert set(run(_claim())) == {a, b}


def test_a_head_that_is_not_due_holds_its_items_queue():
    # The head is backing off (next_attempt_at in the future), so it is not due;
    # the newer, due row for the same item must not jump ahead of it.
    future = utcnow() + timedelta(minutes=5)
    run(_enqueue("500", status="failed", next_attempt_at=future))
    run(_enqueue("500"))                                       # due on its own, but behind the head

    assert run(_claim()) == []
