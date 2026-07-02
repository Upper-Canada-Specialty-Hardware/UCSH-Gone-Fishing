"""Registration + DDL sanity for the migrated business tables.

Feature 1 is purely additive: the new SQLAlchemy models exist and are wired
into Base.metadata, but nothing reads or writes them yet. These tests confirm
(a) every new table is registered so Alembic autogenerate will see it, (b) the
key columns each model is meant to carry are present, and (c) the DDL actually
builds on SQLite (the database the test suite and local dev use) — which
catches column-type mistakes before a migration is ever generated.
"""

from sqlalchemy import create_engine, insert, select

from app.database import Base
from app.models import (
    Employee,
    ManagerAssignment,
    LeaveRequest,
    OvertimeRequest,
    CarryoverPayoutRequest,
    Holiday,
)

NEW_TABLES = {
    "employees",
    "manager_assignments",
    "leave_requests",
    "overtime_requests",
    "carryover_payout_requests",
    "holidays",
}


def test_new_tables_are_registered():
    assert NEW_TABLES <= set(Base.metadata.tables), (
        "missing: " + ", ".join(sorted(NEW_TABLES - set(Base.metadata.tables)))
    )


def test_employee_carries_the_five_pots_and_identity():
    cols = set(Employee.__table__.columns.keys())
    pots = {
        "vacation_balance",
        "sick_balance",
        "overtime_balance",
        "carryover_balance",
        "payout_balance",
    }
    identity = {"sp_item_id", "email", "sp_user_lookup_id", "name"}
    assert pots <= cols
    assert identity <= cols


def test_every_business_table_has_an_sp_origin_key():
    for model in (
        Employee,
        LeaveRequest,
        OvertimeRequest,
        CarryoverPayoutRequest,
        Holiday,
    ):
        assert "sp_item_id" in model.__table__.columns.keys(), model.__tablename__


def test_ddl_builds_on_sqlite_and_round_trips():
    # Build every table in a throwaway in-memory SQLite db, then insert and
    # read back an employee to prove the schema is valid end-to-end.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(
            insert(Employee).values(
                sp_item_id="42",
                email="worker@ucsh.ca",
                name="Test Worker",
                vacation_balance=10.0,
                carryover_balance=2.5,
            )
        )
        row = conn.execute(
            select(Employee.name, Employee.vacation_balance).where(
                Employee.sp_item_id == "42"
            )
        ).one()

    assert row.name == "Test Worker"
    assert row.vacation_balance == 10.0
