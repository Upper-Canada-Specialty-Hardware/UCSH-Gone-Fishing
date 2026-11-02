"""One email subject per request, so every email about it threads in Outlook (#520)."""
import asyncio
import itertools
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.outbox import Outbox
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services.notify_blocked import _blocked_subject
from app.services.request_journal import enqueue_message
from app.services.request_threads import (
    SUBJECT_KEY,
    build_thread_subject,
    stamp_thread_subject,
    thread_subject,
)

_item_ids = itertools.count(95_000_000)


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the request and outbox tables before and after each test."""
    async def wipe():
        async with async_session() as session:
            for model in (Outbox, RequestEvent, RequestRecord):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _request(**overrides) -> RequestRecord:
    """Insert one pending request row and return it."""
    values = dict(
        kind="leave", item_id=next(_item_ids), status="pending",
        employee_id="7", employee_email="casey@ucsh.com", employee_name="Casey Demo",
        leave_type="Vacation", start_date=date(2026, 11, 2), end_date=date(2026, 11, 6),
        approval_version=1, balance_state="not_applied",
    )
    values.update(overrides)
    async with async_session() as session:
        record = RequestRecord(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


# ===== the wording =====


@pytest.mark.parametrize("kind, option, expected", [
    ("leave", None, "Leave request #12 for Casey Demo"),
    ("overtime", None, "Time make-up request #12 for Casey Demo"),
    ("carryover_payout", "carry_over", "Carry-over request #12 for Casey Demo"),
    ("carryover_payout", "payout", "Payout request #12 for Casey Demo"),
    ("carryover_payout", None, "Carry-over or payout request #12 for Casey Demo"),
])
def test_the_subject_names_the_kind_the_number_and_the_person(kind, option, expected):
    assert build_thread_subject(kind, 12, "Casey Demo", option) == expected


def test_a_blank_name_leaves_the_for_part_off():
    assert build_thread_subject("leave", 12, "  ") == "Leave request #12"


# ===== fixed at submit =====


def test_the_stamped_subject_survives_a_later_change_to_the_row():
    record = RequestRecord(kind="carryover_payout", item_id=45, employee_name="Casey Demo",
                           request_option="carry_over", extra={})
    stamp_thread_subject(record)
    record.request_option = "payout"                          # an admin switches it to a payout
    record.employee_name = "Casey Demo-Smith"                 # and the name is corrected
    assert thread_subject(record) == "Carry-over request #45 for Casey Demo"   # the thread is kept


def test_stamping_twice_never_rewrites_the_subject():
    record = RequestRecord(kind="leave", item_id=7, employee_name="Casey Demo", extra={})
    stamp_thread_subject(record)
    record.employee_name = "Someone Else"
    stamp_thread_subject(record)                              # a resubmit or reprocess
    assert record.extra[SUBJECT_KEY] == "Leave request #7 for Casey Demo"


def test_a_row_from_before_the_stamp_gets_the_same_wording_built_from_it():
    record = RequestRecord(kind="overtime", item_id=9, employee_name="Casey Demo", extra={})
    assert thread_subject(record) == "Time make-up request #9 for Casey Demo"


# ===== every queued email uses it =====


def test_every_email_queued_for_a_request_carries_its_one_subject():
    record = run(_request())

    async def queue():
        async with async_session() as session:
            # The callers still build their own step subjects; the queue replaces them.
            for i, (kind, subject) in enumerate([
                ("manager_approval", "Leave Request - Casey Demo"),
                ("receipt", "Leave Request Received - Casey Demo"),
                ("result", "Casey Demo - Leave Request: Approved"),
            ]):
                await enqueue_message(session, record, None, kind, "email", f"p{i}@ucsh.com",
                                      subject, "<p>x</p>", f"k{i}")
            await enqueue_message(session, record, None, "result", "sms", "+15555550101",
                                  None, "approved", "k-sms")
            await session.commit()
        async with async_session() as session:
            return (await session.execute(select(Outbox))).scalars().all()

    rows = run(queue())
    emails = [r for r in rows if r.channel == "email"]
    assert len(emails) == 3
    assert {r.subject for r in emails} == {"Leave request #%s for Casey Demo" % record.item_id}
    assert next(r for r in rows if r.channel == "sms").subject is None    # a text has no subject


def test_an_email_with_no_request_keeps_its_own_subject():
    async def queue():
        async with async_session() as session:
            await enqueue_message(session, None, None, "notice", "email", "a@ucsh.com",
                                  "Something else", "<p>x</p>", "k-none")
            await session.commit()
        async with async_session() as session:
            return (await session.execute(select(Outbox))).scalars().one()

    assert run(queue()).subject == "Something else"


# ===== the blocked-request notice joins the blocked request's thread =====


def test_the_blocked_notice_uses_the_blocked_requests_stored_subject():
    fields = {SUBJECT_KEY: "Leave request #31 for Casey Demo"}
    assert _blocked_subject("leave", "31", fields, "Casey Demo") == "Leave request #31 for Casey Demo"


def test_the_blocked_notice_builds_the_subject_for_an_older_postgres_row():
    assert _blocked_subject("overtime", "32", {}, "Casey Demo") == "Time make-up request #32 for Casey Demo"


def test_the_sharepoint_blocked_notice_keeps_its_old_subject():
    assert _blocked_subject("leave", "33", {}, None) == "Leave Request #33 Cannot Be Approved"
