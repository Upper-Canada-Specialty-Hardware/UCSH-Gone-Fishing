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
from app.models import Employee, Holiday
from app.backfill import mappers
from app.backfill.core import DOMAINS, diff_domain, upsert_domain
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
