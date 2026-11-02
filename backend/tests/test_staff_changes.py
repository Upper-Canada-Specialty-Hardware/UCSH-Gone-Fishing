"""Tests for the staff-change history service (records and reads only).

Run against the local SQLite database conftest builds with create_all; secrets
are stubbed there. Each test starts from an empty staff_changes table.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.staff_change import StaffChange
from app.services import staff_changes


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


async def _clear():
    """Remove every staff_changes row so a test starts clean."""
    async with async_session() as session:
        await session.execute(delete(StaffChange))
        await session.commit()


async def _count(employee_id=None):
    """How many rows exist, for one person or in total."""
    async with async_session() as session:
        query = select(func.count()).select_from(StaffChange)
        if employee_id is not None:
            query = query.where(StaffChange.employee_id == str(employee_id))
        return (await session.execute(query)).scalar_one()


async def _insert_at(employee_id, field, changed_at):
    """Insert one row with an explicit changed_at, to control ordering."""
    async with async_session() as session:
        session.add(StaffChange(
            employee_id=str(employee_id), employee_name="Test Person", field=field,
            old_value=1, new_value=2, reason="setup", source="admin_dashboard",
            changed_at=changed_at,
        ))
        await session.commit()


async def _insert_sp(employee_id, field, old_value, new_value, version, changed_at):
    """Insert one imported (sharepoint) row carrying a version, to test is_start."""
    async with async_session() as session:
        session.add(StaffChange(
            employee_id=str(employee_id), employee_name="Test Person", field=field,
            old_value=old_value, new_value=new_value, reason=None,
            source="sharepoint", version=version, changed_at=changed_at,
        ))
        await session.commit()


@pytest.fixture(autouse=True)
def clean():
    """Empty the table before and after every test."""
    run(_clear())
    yield
    run(_clear())


def test_rows_are_written_with_all_their_values():
    added = run(staff_changes.record_changes(
        employee_id="7", employee_name="Dana Lee",
        changes=[("CurrentVacationBalance", 10.0, 7.5)], reason="used 2.5 days",
    ))
    assert added == 1                                          # one change -> one row
    history = run(staff_changes.list_changes("7"))
    assert len(history) == 1
    row = history[0]
    assert row["field"] == "CurrentVacationBalance"
    assert row["old_value"] == 10.0 and row["new_value"] == 7.5
    assert row["reason"] == "used 2.5 days"
    assert row["source"] == "admin_dashboard"                  # the default source
    assert row["changed_at"].endswith("+00:00")                # ISO 8601 in UTC


def test_numbers_and_none_round_trip_through_json():
    run(staff_changes.record_changes(
        employee_id="7", employee_name="Dana Lee",
        changes=[
            ("CurrentSickBalance", 5, 4),                       # ints stay ints
            ("ManagerLookupId", None, None),                    # None stays None
            ("CurrentVacationBalance", 12.25, None),            # float and a null
        ],
        reason="correction",
    ))
    history = run(staff_changes.list_changes("7"))
    by_field = {r["field"]: r for r in history}
    assert by_field["CurrentSickBalance"]["old_value"] == 5
    assert by_field["CurrentSickBalance"]["new_value"] == 4
    assert by_field["ManagerLookupId"]["old_value"] is None
    assert by_field["ManagerLookupId"]["new_value"] is None
    assert by_field["CurrentVacationBalance"]["old_value"] == 12.25
    assert by_field["CurrentVacationBalance"]["new_value"] is None


def test_history_is_newest_first():
    base = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    run(_insert_at("7", "oldest", base))                        # earliest time
    run(_insert_at("7", "newest", base.replace(hour=14)))       # latest time, inserted second
    run(_insert_at("7", "middle", base.replace(hour=13)))       # middle time, inserted last
    fields = [r["field"] for r in run(staff_changes.list_changes("7"))]
    assert fields == ["newest", "middle", "oldest"]             # ordered by changed_at desc, not insert order


def test_limit_is_honoured_and_keeps_the_newest():
    base = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    for hour in range(5):                                       # five rows, 12:00 .. 16:00
        run(_insert_at("7", f"h{hour}", base.replace(hour=12 + hour)))
    history = run(staff_changes.list_changes("7", limit=2))
    assert len(history) == 2                                    # only two returned
    assert [r["field"] for r in history] == ["h4", "h3"]        # the two newest


def test_another_employees_rows_are_not_returned():
    run(staff_changes.record_changes(
        employee_id="7", employee_name="Dana Lee",
        changes=[("CurrentVacationBalance", 1, 2)], reason="hers",
    ))
    run(staff_changes.record_changes(
        employee_id="8", employee_name="Sam Roy",
        changes=[("CurrentVacationBalance", 3, 4)], reason="his",
    ))
    history = run(staff_changes.list_changes("7"))
    assert len(history) == 1                                    # only employee 7's row
    assert history[0]["reason"] == "hers"


def test_employee_id_is_compared_as_a_string():
    run(staff_changes.record_changes(
        employee_id=7, employee_name="Dana Lee",               # int id in
        changes=[("CurrentVacationBalance", 1, 2)], reason="int id",
    ))
    assert len(run(staff_changes.list_changes(7))) == 1         # int lookup
    assert len(run(staff_changes.list_changes("7"))) == 1       # and string lookup find it


def test_empty_changes_returns_zero_and_writes_nothing():
    added = run(staff_changes.record_changes(
        employee_id="7", employee_name="Dana Lee", changes=[], reason="nothing changed",
    ))
    assert added == 0                                          # nothing to do
    assert run(_count()) == 0                                  # and nothing written anywhere


def test_a_blank_reason_raises_and_writes_nothing():
    with pytest.raises(ValueError):
        run(staff_changes.record_changes(
            employee_id="7", employee_name="Dana Lee",
            changes=[("CurrentVacationBalance", 1, 2)], reason="   ",   # whitespace only
        ))
    assert run(_count("7")) == 0                                # the guard ran before any insert


def test_a_failing_insert_writes_nothing():
    with pytest.raises(Exception):                             # the set is not JSON-serialisable
        run(staff_changes.record_changes(
            employee_id="7", employee_name="Dana Lee",
            changes=[
                ("CurrentVacationBalance", 1, 2),              # a valid change, first in the batch
                ("CurrentSickBalance", 3, {"unserialisable"}),  # a set breaks JSON encoding
            ],
            reason="all or nothing",
        ))
    assert run(_count("7")) == 0                                # the valid row rolled back with the bad one


# ----- is_start marks the oldest imported version's seed rows (#448) -----


def test_is_start_marks_only_the_oldest_imported_version_rows():
    """is_start flags the oldest imported version's seeds, not a later blank->value row."""
    base = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    # Oldest kept version 2.0 seeds two fields (no "before") -> start rows.
    run(_insert_sp("7", "CurrentVacationBalance", None, 10, "2.0", base))
    run(_insert_sp("7", "SalaryHourly", None, "Salary", "2.0", base))
    # A later version 5.0: a field that was blank gains a value (old value null
    # too), which must NOT read as a start row, and a real before->after change.
    run(_insert_sp("7", "CarryOver", None, 3, "5.0", base.replace(hour=14)))
    run(_insert_sp("7", "CurrentVacationBalance", 10, 7, "5.0", base.replace(hour=14)))

    by = {(r["field"], r["version"]): r for r in run(staff_changes.list_changes("7"))}
    assert by[("CurrentVacationBalance", "2.0")]["is_start"] is True       # oldest version seed
    assert by[("SalaryHourly", "2.0")]["is_start"] is True                 # oldest version seed
    assert by[("CarryOver", "5.0")]["is_start"] is False                   # null old, but a later version
    assert by[("CurrentVacationBalance", "5.0")]["is_start"] is False      # a real change


def test_an_app_edit_is_never_a_start_row():
    """An app edit (no version, source admin_dashboard) is never a start row, null old or not."""
    run(staff_changes.record_changes("7", "Dana Lee", [("CurrentVacationBalance", None, 5)], "set it"))
    row = run(staff_changes.list_changes("7"))[0]
    assert row["is_start"] is False                                        # null old, but an app edit
