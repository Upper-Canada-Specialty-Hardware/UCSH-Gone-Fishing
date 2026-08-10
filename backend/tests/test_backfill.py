"""Tests for the SharePoint -> Postgres backfill/verify tool (migration PR D).

Three things must hold before any cutover trusts this tool:
  1. the SP-field -> PG-column mappers translate correctly, including the two
     Person/Group field shapes and type coercion (dates, numbers);
  2. the upsert is idempotent — re-running never duplicates a row and picks up
     changed values; and
  3. the read-only verify diff catches missing rows, field drift, and orphans.

All against in-memory SQLite + hand-built SP items — no Graph, no live data.
"""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Employee, Holiday, ManagerAssignment
from app.backfill import mappers
from app.backfill.core import (
    DOMAINS,
    diff_domain,
    diff_manager_assignments,
    resolve_domains,
    upsert_domain,
    upsert_manager_assignments,
)
from app.backfill.__main__ import main


async def _make_sessionmaker():
    """A persistent in-memory SQLite (StaticPool keeps the one connection so the
    schema survives across sessions) with every business table created."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# --------------------------- mappers (pure) ---------------------------

def test_map_holiday_parses_date():
    row = mappers.map_holiday(
        {"id": 7, "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01T00:00:00Z"}}
    )
    assert row["sp_item_id"] == "7"          # coerced to str
    assert row["title"] == "Canada Day"
    assert str(row["date"]) == "2026-07-01"  # ISO+Z -> date
    assert row["province"] == "ON"


def test_map_employee_coerces_balances_and_defaults_blanks_to_zero():
    row = mappers.map_employee({
        "id": "3",
        "fields": {
            "Title": "Jo Worker", "EmailAddress": "jo@ucsh.ca", "Location": "Toronto",
            "CurrentVacationBalance": "8.5", "CurrentSickDayBalance": 4,
            "CurrentOvertimeBalance": "", "CarryOver": None, "Payout": "0",
            "DefaultYearlyVacationDays": "15",
        },
    })
    assert row["name"] == "Jo Worker"
    assert row["vacation_balance"] == 8.5     # string -> float
    assert row["sick_balance"] == 4.0
    assert row["overtime_balance"] == 0.0     # "" -> 0.0 (NOT NULL column)
    assert row["carryover_balance"] == 0.0    # None -> 0.0
    assert row["vacation_entitlement"] == 15.0
    assert row["sp_user_lookup_id"] is None   # identity linkage deferred to PR F


def test_extract_lookup_id_handles_both_person_field_shapes():
    # form-created: explicit "<prefix>LookupId" scalar
    assert mappers.map_leave_request(
        {"id": "1", "fields": {"SubmittedTestLookupId": "42", "ManagerLookupId": 9}}
    )["submitter_sp_user_lookup_id"] == 42
    # SP-created: nested {"LookupId": ...} object
    ot = mappers.map_overtime_request(
        {"id": "2", "fields": {"SubmittedBy": {"LookupId": "17"}, "Manager": {"LookupId": 5}}}
    )
    assert ot["submitter_sp_user_lookup_id"] == 17
    assert ot["manager_sp_user_lookup_id"] == 5
    # absent -> None
    assert mappers.map_overtime_request({"id": "3", "fields": {}})["manager_sp_user_lookup_id"] is None


# --------------------------- upsert (idempotent) ---------------------------

def test_upsert_is_idempotent_and_updates_in_place():
    def _run():
        async def inner():
            Session = await _make_sessionmaker()
            domain = DOMAINS["holidays"]
            items = [
                {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
                {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
            ]
            async with Session() as s:
                first = await upsert_domain(s, domain, items)
            # Re-run with one value changed: no new rows, the change is applied.
            items[0]["fields"]["Title"] = "Canada Day (obs)"
            async with Session() as s:
                second = await upsert_domain(s, domain, items)
            async with Session() as s:
                rows = (await s.execute(select(Holiday))).scalars().all()
                titles = {r.sp_item_id: r.title for r in rows}
            return first, second, len(rows), titles
        return asyncio.run(inner())

    first, second, count, titles = _run()
    assert first == {"total_sharepoint": 2, "inserted": 2, "updated": 0}
    assert second == {"total_sharepoint": 2, "inserted": 0, "updated": 2}
    assert count == 2                              # re-run did NOT duplicate
    assert titles["1"] == "Canada Day (obs)"       # in-place update applied


# --------------------------- verify diff ---------------------------

def test_diff_reports_parity_then_drift_missing_and_orphan():
    def _run():
        async def inner():
            Session = await _make_sessionmaker()
            domain = DOMAINS["employees"]
            sp_items = [
                {"id": "1", "fields": {"Title": "A", "CurrentVacationBalance": "10"}},
                {"id": "2", "fields": {"Title": "B", "CurrentVacationBalance": "5"}},
            ]
            async with Session() as s:
                await upsert_domain(s, domain, sp_items)

            # 1) right after backfill -> full parity
            async with Session() as s:
                clean = await diff_domain(s, domain, sp_items)

            # 2) mutate a PG row (drift) -> field_mismatches
            async with Session() as s:
                emp = (await s.execute(select(Employee).where(Employee.sp_item_id == "1"))).scalar_one()
                emp.vacation_balance = 999.0
                await s.commit()
            async with Session() as s:
                drifted = await diff_domain(s, domain, sp_items)

            # 3) SP gains a new item not in PG (missing) and drops "2" (orphan)
            sp_next = [
                {"id": "1", "fields": {"Title": "A", "CurrentVacationBalance": "10"}},
                {"id": "3", "fields": {"Title": "C", "CurrentVacationBalance": "7"}},
            ]
            async with Session() as s:
                gapped = await diff_domain(s, domain, sp_next)
            return clean, drifted, gapped
        return asyncio.run(inner())

    clean, drifted, gapped = _run()

    assert clean["in_parity"] is True
    assert clean["missing_in_postgres"] == [] and clean["orphans_in_postgres"] == []

    assert drifted["in_parity"] is False
    assert drifted["field_mismatches"][0]["sp_item_id"] == "1"
    assert "vacation_balance" in drifted["field_mismatches"][0]["fields"]

    assert gapped["in_parity"] is False
    assert gapped["missing_in_postgres"] == ["3"]   # in SP, not yet in PG
    assert gapped["orphans_in_postgres"] == ["2"]   # in PG, no longer in SP


# ------------------- derived domain: manager_assignments -------------------

def test_map_manager_assignments_keeps_order_and_drops_unusable_entries():
    rows = mappers.map_manager_assignments({
        "id": "1",
        "fields": {"AllManagers": [
            {"LookupId": 11, "LookupValue": "Primary Boss"},
            {"LookupId": "22", "LookupValue": ""},   # str id; blank name
            {"LookupId": 11, "LookupValue": "Dupe"}, # would break the unique constraint
            {"LookupValue": "No id at all"},         # unusable — no LookupId
            "not-a-dict",
        ]},
    })
    # Order preserved and positions contiguous despite the skipped entries.
    assert [(r["manager_sp_user_lookup_id"], r["position"]) for r in rows] == [(11, 0), (22, 1)]
    assert rows[0]["manager_name"] == "Primary Boss"
    assert rows[1]["manager_name"] is None            # "" normalizes to None
    # No managers / absent field -> no edges rather than an error.
    assert mappers.map_manager_assignments({"id": "2", "fields": {}}) == []


def _employee_item(sp_id, name, managers):
    """A Staff Directory item carrying an AllManagers person field."""
    return {"id": sp_id, "fields": {"Title": name, "AllManagers": managers}}


def test_manager_assignments_replace_the_set_and_are_idempotent():
    """A manager removed in SharePoint must be removed here, not just skipped.

    Insert-only would leave an ex-manager approving that employee's requests
    forever, so the upsert replaces each employee's whole set of edges.
    """
    def _run():
        async def inner():
            Session = await _make_sessionmaker()
            sp_items = [_employee_item("1", "Jo Worker", [
                {"LookupId": 11, "LookupValue": "Boss One"},
                {"LookupId": 22, "LookupValue": "Boss Two"},
            ])]
            async with Session() as s:
                await upsert_domain(s, DOMAINS["employees"], sp_items)  # FK targets first
                first = await upsert_manager_assignments(s, sp_items)
            async with Session() as s:
                second = await upsert_manager_assignments(s, sp_items)  # unchanged re-run

            # SharePoint drops Boss Two and re-titles the survivor.
            sp_items[0]["fields"]["AllManagers"] = [{"LookupId": 22, "LookupValue": "Boss Two Renamed"}]
            async with Session() as s:
                third = await upsert_manager_assignments(s, sp_items)
            async with Session() as s:
                rows = (await s.execute(select(ManagerAssignment))).scalars().all()
                remaining = [(r.manager_sp_user_lookup_id, r.position, r.manager_name) for r in rows]
            return first, second, third, remaining
        return asyncio.run(inner())

    first, second, third, remaining = _run()
    assert (first["inserted"], first["deleted"]) == (2, 0)
    assert (second["inserted"], second["updated"], second["deleted"]) == (0, 2, 0)
    assert second["employees_missing_in_postgres"] == []
    assert third["deleted"] == 1                       # Boss One's edge removed
    # Survivor kept, renamed, and promoted to primary (position 0).
    assert remaining == [(22, 0, "Boss Two Renamed")]


def test_manager_assignment_diff_reports_parity_then_missing_and_orphan():
    def _run():
        async def inner():
            Session = await _make_sessionmaker()
            sp_items = [_employee_item("1", "Jo Worker", [{"LookupId": 11, "LookupValue": "Boss One"}])]
            async with Session() as s:
                await upsert_domain(s, DOMAINS["employees"], sp_items)
                await upsert_manager_assignments(s, sp_items)
            async with Session() as s:
                clean = await diff_manager_assignments(s, sp_items)

            # SharePoint swaps the manager: 99 is missing in PG, 11 is now orphaned.
            swapped = [_employee_item("1", "Jo Worker", [{"LookupId": 99, "LookupValue": "New Boss"}])]
            async with Session() as s:
                gapped = await diff_manager_assignments(s, swapped)
            return clean, gapped
        return asyncio.run(inner())

    clean, gapped = _run()
    assert clean["in_parity"] is True

    assert gapped["in_parity"] is False
    assert gapped["missing_in_postgres"] == [
        {"employee_sp_item_id": "1", "manager_sp_user_lookup_id": 99}
    ]
    # Reported by SP item id, not the internal employees.id primary key.
    assert gapped["orphans_in_postgres"] == [
        {"employee_sp_item_id": "1", "manager_sp_user_lookup_id": 11}
    ]


def test_manager_assignment_diff_fails_parity_when_employees_are_not_backfilled():
    """The gap that would silently break approval routing.

    Edges cannot be derived without an employees row to point at. If verify
    passed in that state, flipping the employees flag would serve every employee
    with zero managers and requests would reach no approver.
    """
    def _run():
        async def inner():
            Session = await _make_sessionmaker()
            sp_items = [_employee_item("1", "Jo Worker", [{"LookupId": 11, "LookupValue": "Boss One"}])]
            async with Session() as s:                    # note: employees NOT upserted
                report = await diff_manager_assignments(s, sp_items)
                applied = await upsert_manager_assignments(s, sp_items)
                rows = (await s.execute(select(ManagerAssignment))).scalars().all()
            return report, applied, len(rows)
        return asyncio.run(inner())

    report, applied, row_count = _run()
    assert report["employees_missing_in_postgres"] == ["1"]
    assert report["in_parity"] is False                   # gates the cutover
    # Apply skips rather than crashing, and writes nothing it cannot key.
    assert applied["employees_missing_in_postgres"] == ["1"]
    assert row_count == 0


def test_resolve_domains_runs_employees_before_manager_assignments():
    """Dependency order beats the order the flags were passed in."""
    ordered = [d.name for d in resolve_domains(["manager_assignments", "employees"])]
    assert ordered == ["employees", "manager_assignments"]
    # A default (all-domains) run must satisfy the same constraint.
    all_names = [d.name for d in resolve_domains(None)]
    assert all_names.index("employees") < all_names.index("manager_assignments")


# --------------------------- CLI exit-code gate ---------------------------

def test_cli_verify_exits_nonzero_on_drift(monkeypatch):
    async def fake_run(domain_names=None, apply=False):
        return {"mode": "verify", "domains": {"holidays": {"in_parity": False}}}
    monkeypatch.setattr("app.backfill.__main__.run", fake_run)
    assert main(["--domain", "holidays"]) == 1


def test_cli_verify_exits_zero_on_parity(monkeypatch):
    async def fake_run(domain_names=None, apply=False):
        return {"mode": "verify", "domains": {"holidays": {"in_parity": True}}}
    monkeypatch.setattr("app.backfill.__main__.run", fake_run)
    assert main([]) == 0


def test_cli_apply_exits_zero(monkeypatch):
    async def fake_run(domain_names=None, apply=False):
        assert apply is True
        return {"mode": "apply", "domains": {"holidays": {"inserted": 1}}}
    monkeypatch.setattr("app.backfill.__main__.run", fake_run)
    assert main(["--apply"]) == 0
