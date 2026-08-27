"""Migration: the three request lists can be served from Postgres.

The cutover is only safe if a caller cannot tell Postgres from SharePoint, so
most of what is asserted is *shape* — the SharePoint field names each service
reads and writes, the per-list differences (leave's SubmittedTest person field
and ApproveProcessedFlag vs carryover's SubmittedBy, Status *and* SystemState),
ISO date strings, and that a write of an unmapped field raises rather than being
silently dropped.

Runs against the local SQLite fallback (DATABASE_URL is empty under conftest).
"""
import asyncio
from datetime import date

import pytest

from app.database import Base, async_session, engine
from app.models.carryover_payout_request import CarryoverPayoutRequest
from app.models.leave_request import LeaveRequest
from app.models.overtime_request import OvertimeRequest
from app.repositories.postgres.requests import (
    CARRYOVER, LEAVE, OVERTIME, PostgresRequestRepository,
)


def _reset_and_seed(seed):
    """Rebuild the request tables and run the async `seed` coroutine factory."""
    async def inner():
        async with engine.begin() as conn:
            for model in (LeaveRequest, OvertimeRequest, CarryoverPayoutRequest):
                await conn.run_sync(model.__table__.drop, checkfirst=True)
            await conn.run_sync(Base.metadata.create_all)
        async with async_session() as session:
            await seed(session)
            await session.commit()
    asyncio.run(inner())


# --- Leave: shape parity, person field, dates -------------------------------

def test_leave_round_trips_every_field_a_service_reads():
    async def seed(s):
        s.add(LeaveRequest(
            sp_item_id="100", leave_type="Vacation", status="Pending",
            approve_processed_flag="Not Processed",
            start_date=date(2027, 3, 1), end_date=date(2027, 3, 3), days=3.0,
            title="Jane — vacation", notes="trip",
            submitter_sp_user_lookup_id=42, manager_sp_user_lookup_id=7,
            staff_location="Toronto Warden", staff_department="Warehouse",
        ))
    _reset_and_seed(seed)
    fields = asyncio.run(PostgresRequestRepository(LEAVE).get_by_id("100"))["fields"]

    assert fields["LeaveType"] == "Vacation"
    assert fields["Status"] == "Pending"
    assert fields["ApproveProcessedFlag"] == "Not Processed"
    assert fields["StartDate"] == "2027-03-01"   # emitted as ISO string, like Graph
    assert fields["EndDate"] == "2027-03-03"
    assert fields["Days"] == 3.0
    # Person field is SubmittedTest for leave; resolve_person_field reads the
    # *LookupId suffix, so that is what must be populated.
    assert fields["SubmittedTestLookupId"] == 42
    assert fields["ManagerLookupId"] == 7


def test_leave_approval_write_round_trips():
    async def seed(s):
        s.add(LeaveRequest(sp_item_id="101", status="Pending"))
    _reset_and_seed(seed)
    repo = PostgresRequestRepository(LEAVE)
    asyncio.run(repo.update_fields("101", {
        "Status": "Approved", "ApproveProcessedFlag": "Processed",
        "ApprovedDate": "2027-03-05",
    }))
    fields = asyncio.run(repo.get_by_id("101"))["fields"]
    assert fields["Status"] == "Approved"
    assert fields["ApprovedDate"] == "2027-03-05"   # 0008 column


# --- Overtime: its own person field + StartDate-as-day-worked ----------------

def test_overtime_shape_and_submittedby_field():
    async def seed(s):
        s.add(OvertimeRequest(
            sp_item_id="200", title="Weekend load-in", date=date(2027, 4, 4),
            hours=6.0, status="Pending", submitter_sp_user_lookup_id=55,
        ))
    _reset_and_seed(seed)
    fields = asyncio.run(PostgresRequestRepository(OVERTIME).get_by_id("200"))["fields"]
    assert fields["Hours"] == 6.0
    assert fields["StartDate"] == "2027-04-04"        # the day worked
    assert fields["SubmittedByLookupId"] == 55        # overtime uses SubmittedBy, not SubmittedTest


# --- Carryover: Status AND SystemState, EmployeeID stringified ---------------

def test_carryover_carries_both_status_and_system_state():
    async def seed(s):
        s.add(CarryoverPayoutRequest(
            sp_item_id="300", type_of_request="Payout", days=5.0,
            system_state="Not Processed", status="Pending",
            employee_sp_item_id="12", submitter_sp_user_lookup_id=42,
        ))
    _reset_and_seed(seed)
    fields = asyncio.run(PostgresRequestRepository(CARRYOVER).get_by_id("300"))["fields"]
    assert fields["TypeofRequest"] == "Payout"
    assert fields["SystemState"] == "Not Processed"
    assert fields["Status"] == "Pending"
    assert fields["EmployeeID"] == "12"


def test_carryover_employee_id_is_stored_as_string_even_from_int():
    # The service writes EmployeeID as int(); the column is String, which Postgres
    # would reject without coercion.
    async def seed(s):
        s.add(CarryoverPayoutRequest(sp_item_id="301", system_state="Not Processed"))
    _reset_and_seed(seed)
    repo = PostgresRequestRepository(CARRYOVER)
    asyncio.run(repo.update_fields("301", {"EmployeeID": 12, "ManagerLookupId": 7}))
    fields = asyncio.run(repo.get_by_id("301"))["fields"]
    assert fields["EmployeeID"] == "12"
    assert fields["ManagerLookupId"] == 7


# --- Create + guards --------------------------------------------------------

def test_create_mints_an_id_above_the_max():
    async def seed(s):
        s.add(LeaveRequest(sp_item_id="100", status="Pending"))
    _reset_and_seed(seed)
    created = asyncio.run(PostgresRequestRepository(LEAVE).create({
        "LeaveType": "Vacation", "Status": "Pending", "StartDate": "2027-06-01",
        "EndDate": "2027-06-02", "SubmittedTestLookupId": 42,
    }))
    assert created["id"] == "101"
    assert created["fields"]["LeaveType"] == "Vacation"


def test_unknown_field_raises_rather_than_being_dropped():
    _reset_and_seed(lambda s: _noop())
    with pytest.raises(KeyError) as excinfo:
        asyncio.run(PostgresRequestRepository(LEAVE).create({"Status": "Pending", "Bogus": 1}))
    assert "Bogus" in str(excinfo.value)


def test_get_missing_request_raises():
    _reset_and_seed(lambda s: _noop())
    with pytest.raises(KeyError):
        asyncio.run(PostgresRequestRepository(OVERTIME).get_by_id("999"))


async def _noop():
    return None
