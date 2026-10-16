"""The typed adapter behaves like a SharePoint request list over real columns (#321).

TypedRequestList reads and writes the typed ``requests`` table but answers in
Graph's list-item shapes. These tests cover: the CHECK constraints refusing a
malformed row, a round trip of SharePoint-named fields through the adapter, the
leave Title rebuilt on read, unknown columns kept in ``extra``, and item_ids
drawn per kind.
"""

import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList

LEAVE = "leave-list-id"           # the adapter ignores the list id beyond error messages


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def clean():
    """Empty the typed request tables before and after each test."""

    async def wipe():
        async with async_session() as session:
            await session.execute(delete(RequestChange))
            await session.execute(delete(RequestRecord))
            await session.execute(delete(RequestCounter))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _insert(record: RequestRecord):
    """Try to commit one record; let any IntegrityError propagate."""
    async with async_session() as session:
        session.add(record)
        await session.commit()


def test_a_leave_row_without_a_start_date_is_refused(clean):
    # ck_requests_leave_fields: a leave needs both a leave type and a start date.
    bad = RequestRecord(kind="leave", item_id=1, employee_id="7", leave_type="Vacation")
    with pytest.raises(IntegrityError):
        run(_insert(bad))


def test_a_bad_status_is_refused(clean):
    # ck_requests_status: only the five lifecycle values are allowed.
    bad = RequestRecord(
        kind="leave", item_id=2, employee_id="7", leave_type="Vacation",
        start_date=date(2026, 11, 2), status="weird",
    )
    with pytest.raises(IntegrityError):
        run(_insert(bad))


def test_negative_days_are_refused(clean):
    # ck_requests_days_nonneg: an amount of days is never below zero.
    bad = RequestRecord(
        kind="leave", item_id=3, employee_id="7", leave_type="Vacation",
        start_date=date(2026, 11, 2), days=-1,
    )
    with pytest.raises(IntegrityError):
        run(_insert(bad))


def test_an_overtime_row_needs_positive_hours(clean):
    # ck_requests_overtime_fields: overtime needs a date worked and hours > 0.
    bad = RequestRecord(kind="overtime", item_id=4, employee_id="7", start_date=date(2026, 11, 2), hours=0)
    with pytest.raises(IntegrityError):
        run(_insert(bad))


def test_a_leave_round_trips_through_the_adapter(clean):
    leave = TypedRequestList("leave")
    created = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02",
        "EndDate": "2026-11-03", "Days": 2.0, "SubmitterEmail": "Pat@UCSH.com",
        "EmployeeID": "7", "ManagerLookupId": "3", "WeirdColumn": "kept",
    }))
    assert isinstance(created["id"], str)                       # Graph sends ids as strings
    fetched = run(leave.get_list_item(LEAVE, created["id"]))
    fields = fetched["fields"]
    assert fields["LeaveType"] == "Vacation"
    assert fields["Status"] == "Pending"                        # title case on the wire
    assert fields["StartDate"] == "2026-11-02" and fields["EndDate"] == "2026-11-03"
    assert fields["Days"] == 2.0
    assert fields["SubmitterEmail"] == "pat@ucsh.com"           # lower-cased on store
    assert fields["EmployeeID"] == "7"
    assert fields["id"] == created["id"]


def test_unknown_columns_are_kept_in_extra(clean):
    leave = TypedRequestList("leave")
    created = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02",
        "EmployeeID": "7", "ManagerLookupId": "3", "WeirdColumn": "kept",
    }))
    # The adapter has no column for these, so they come back unchanged.
    fields = run(leave.get_list_item(LEAVE, created["id"]))["fields"]
    assert fields["ManagerLookupId"] == "3" and fields["WeirdColumn"] == "kept"

    async def read_extra():
        async with async_session() as session:
            row = (await session.execute(select(RequestRecord))).scalars().one()
            return row.extra

    assert run(read_extra()) == {"ManagerLookupId": "3", "WeirdColumn": "kept"}


def test_an_update_merges_and_records_a_change(clean):
    leave = TypedRequestList("leave")
    created = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02",
        "Days": 1.0, "EmployeeID": "7",
    }))
    fields = run(leave.update_list_item_fields(LEAVE, created["id"], {"Status": "Approved"}))
    assert fields["Status"] == "Approved"
    assert fields["Days"] == 1.0                                # untouched field kept, like a PATCH

    async def status_changes():
        async with async_session() as session:
            rows = (await session.execute(
                select(RequestChange).where(RequestChange.field == "Status")
            )).scalars().all()
            return [(r.old_value, r.new_value) for r in rows]

    # One change on create (None -> Pending) and one on update (Pending -> Approved).
    assert ("Pending", "Approved") in run(status_changes())


def test_the_leave_title_is_rebuilt_on_read(clean):
    # A record whose name and description are stored separately (as the import or
    # approval path sets them) rebuilds into the compound Title on read.
    async def seed():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="leave", item_id=4100, employee_id="7", employee_name="Pat Staff",
                leave_type="Vacation", start_date=date(2026, 11, 2), description="Doctor", status="pending",
            ))
            await session.commit()

    run(seed())
    fields = run(TypedRequestList("leave").get_list_item(LEAVE, "4100"))["fields"]
    assert fields["Title"] == "Pat Staff /// Doctor"


def test_a_form_title_is_split_into_description_and_relation(clean):
    leave = TypedRequestList("leave")
    created = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Bereavement", "Status": "Pending", "StartDate": "2026-11-02",
        "EmployeeID": "7", "Title": "Grandmother passed /// Grandmother",
    }))

    async def read_row():
        async with async_session() as session:
            return (await session.execute(
                select(RequestRecord).where(RequestRecord.item_id == int(created["id"]))
            )).scalars().one()

    row = run(read_row())
    assert row.description == "Grandmother passed" and row.bereavement_relation == "Grandmother"


def test_carryover_and_overtime_round_trip(clean):
    carry = TypedRequestList("carryover_payout")
    created = run(carry.create_list_item(LEAVE, {
        "TypeofRequest": "Carry Over", "Days": 5.0, "Status": "Pending", "EmployeeID": "7",
    }))
    fields = run(carry.get_list_item(LEAVE, created["id"]))["fields"]
    assert fields["TypeofRequest"] == "Carry Over"              # stored carry_over, shown "Carry Over"

    overtime = TypedRequestList("overtime")
    ot = run(overtime.create_list_item(LEAVE, {
        "Date": "2026-11-02", "Hours": 4.0, "Status": "Pending", "EmployeeID": "7", "Title": "Late shift",
    }))
    ot_fields = run(overtime.get_list_item(LEAVE, ot["id"]))["fields"]
    assert ot_fields["StartDate"] == "2026-11-02" and ot_fields["Hours"] == 4.0
    assert ot_fields["Title"] == "Late shift"                   # overtime Title is the description as-is


def test_item_ids_count_per_kind(clean):
    leave = TypedRequestList("leave")
    first = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02", "EmployeeID": "7",
    }))
    second = run(leave.create_list_item(LEAVE, {
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-03", "EmployeeID": "7",
    }))
    overtime = run(TypedRequestList("overtime").create_list_item(LEAVE, {
        "Date": "2026-11-02", "Hours": 4.0, "Status": "Pending", "EmployeeID": "7",
    }))
    assert int(second["id"]) == int(first["id"]) + 1            # leave numbers run on
    assert overtime["id"] == first["id"]                        # overtime counts on its own, from the same start
