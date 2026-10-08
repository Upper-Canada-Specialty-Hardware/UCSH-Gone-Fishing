"""The one-time SharePoint -> Postgres holiday copy, and the parity check.

Covers copy_holidays_from_sharepoint (idempotent upsert keyed on the SharePoint
item id) and holidays_parity (compares both backends without writing), plus the
two routes: GET /admin/holidays/parity (read-only, ungated) and POST
/admin/holidays/copy-to-postgres (gated behind PROCESSING_ENABLED like the other
admin writes).

The SharePoint side is faked (no Graph); the Postgres side is the local SQLite
fallback (DATABASE_URL is empty under conftest).
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.database import Base, async_session, engine
from app.models.holiday import Holiday
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.services import holidays as holidays_service

# SharePoint-shaped rows the fake SharePoint repo will return. The Date carries a
# Graph-style timestamp on purpose, to prove parity normalises it to a plain date.
SP_ITEMS = [
    {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01T00:00:00Z"}},
    {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03T00:00:00Z"}},
    {"id": "3", "fields": {"Title": "Half Fridays START", "Province": "ON", "Date": "2026-06-01T00:00:00Z"}},
]


class _FakeSharePointHolidayRepository:
    """SharePoint holiday repo stand-in: returns SP_ITEMS, no network."""

    async def get_all(self):
        return SP_ITEMS


def _seed_postgres(items):
    """Replace the Postgres holidays table with the given {"id","fields"} items.

    Args:
        items: Holiday items in the SharePoint shape (Date as plain YYYY-MM-DD).
    """
    async def inner():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session() as session:
            from datetime import date
            for existing in (await session.execute(select(Holiday))).scalars():
                await session.delete(existing)
            await session.commit()
            for item in items:
                f = item["fields"]
                session.add(Holiday(
                    sp_item_id=item["id"], title=f.get("Title"),
                    province=f.get("Province"),
                    date=date.fromisoformat(f["Date"]) if f.get("Date") else None,
                ))
            await session.commit()
    asyncio.run(inner())


@pytest.fixture
def fake_sharepoint(monkeypatch):
    """Point the copy/parity service at the fake SharePoint repo."""
    monkeypatch.setattr(holidays_service, "SharePointHolidayRepository", _FakeSharePointHolidayRepository)


# --- copy_holidays_from_sharepoint ------------------------------------------

def test_copy_inserts_every_sharepoint_row_keyed_on_its_sp_id(fake_sharepoint):
    _seed_postgres([])                                   # start from an empty table
    summary = asyncio.run(holidays_service.copy_holidays_from_sharepoint())

    assert summary == {"source_count": 3, "created": 3, "updated": 0}
    rows = asyncio.run(PostgresHolidayRepository().get_all())
    # Ids are preserved from SharePoint, not minted fresh.
    assert {r["id"] for r in rows} == {"1", "2", "3"}
    # The Graph timestamp is stored as a plain date and re-emitted as YYYY-MM-DD.
    canada = next(r for r in rows if r["id"] == "1")
    assert canada["fields"]["Date"] == "2026-07-01"


def test_copy_is_idempotent_and_updates_rather_than_duplicates(fake_sharepoint):
    _seed_postgres([])
    asyncio.run(holidays_service.copy_holidays_from_sharepoint())   # first run inserts
    second = asyncio.run(holidays_service.copy_holidays_from_sharepoint())  # second run updates

    assert second == {"source_count": 3, "created": 0, "updated": 3}
    rows = asyncio.run(PostgresHolidayRepository().get_all())
    assert len(rows) == 3                                # no duplicate rows


# --- holidays_parity --------------------------------------------------------

def test_parity_is_clean_after_a_copy(fake_sharepoint):
    _seed_postgres([])
    asyncio.run(holidays_service.copy_holidays_from_sharepoint())

    report = asyncio.run(holidays_service.holidays_parity())
    assert report["in_parity"] is True
    assert report["sharepoint_count"] == 3
    assert report["postgres_count"] == 3
    assert report["only_in_sharepoint"] == []
    assert report["only_in_postgres"] == []
    assert report["mismatched"] == []


def test_parity_flags_a_row_missing_from_postgres(fake_sharepoint):
    # Postgres has only two of the three SharePoint rows.
    _seed_postgres([
        {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
        {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
    ])
    report = asyncio.run(holidays_service.holidays_parity())
    assert report["in_parity"] is False
    assert report["only_in_sharepoint"] == ["3"]


def test_parity_flags_a_stale_row_only_in_postgres(fake_sharepoint):
    # Postgres carries an extra row SharePoint no longer has.
    _seed_postgres([
        {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
        {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
        {"id": "3", "fields": {"Title": "Half Fridays START", "Province": "ON", "Date": "2026-06-01"}},
        {"id": "99", "fields": {"Title": "Deleted Day", "Province": "ON", "Date": "2026-12-25"}},
    ])
    report = asyncio.run(holidays_service.holidays_parity())
    assert report["in_parity"] is False
    assert report["only_in_postgres"] == ["99"]


def test_parity_reports_a_field_mismatch(fake_sharepoint):
    # Same ids, but row 1's title differs between the two backends.
    _seed_postgres([
        {"id": "1", "fields": {"Title": "Canada Day (old)", "Province": "ON", "Date": "2026-07-01"}},
        {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
        {"id": "3", "fields": {"Title": "Half Fridays START", "Province": "ON", "Date": "2026-06-01"}},
    ])
    report = asyncio.run(holidays_service.holidays_parity())
    assert report["in_parity"] is False
    assert [m["id"] for m in report["mismatched"]] == ["1"]
    assert "Title" in report["mismatched"][0]["differences"]


def test_parity_treats_a_graph_timestamp_and_a_plain_date_as_equal(fake_sharepoint):
    # Postgres stores plain dates; SharePoint returns "...T00:00:00Z". Equal.
    _seed_postgres([
        {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
        {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
        {"id": "3", "fields": {"Title": "Half Fridays START", "Province": "ON", "Date": "2026-06-01"}},
    ])
    report = asyncio.run(holidays_service.holidays_parity())
    assert report["in_parity"] is True


# --- Routes -----------------------------------------------------------------

@pytest.fixture
def client():
    from app.routes.dashboard import router as dashboard_router
    app = FastAPI()
    app.include_router(dashboard_router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


def test_parity_route_is_read_only_and_runs_while_processing_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)

    async def _parity():
        return {"in_parity": True, "sharepoint_count": 0, "postgres_count": 0,
                "only_in_sharepoint": [], "only_in_postgres": [], "mismatched": []}

    monkeypatch.setattr(holidays_service, "holidays_parity", _parity)
    resp = client.get("/api/dashboard/admin/holidays/parity")
    assert resp.status_code == 200
    assert resp.json()["in_parity"] is True


def test_copy_route_is_refused_while_processing_is_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    assert client.post("/api/dashboard/admin/holidays/copy-to-postgres").status_code == 503


def test_copy_route_runs_when_processing_is_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def _copy():
        return {"source_count": 2, "created": 2, "updated": 0}

    monkeypatch.setattr(holidays_service, "copy_holidays_from_sharepoint", _copy)
    resp = client.post("/api/dashboard/admin/holidays/copy-to-postgres")
    assert resp.status_code == 200
    assert resp.json() == {"source_count": 2, "created": 2, "updated": 0}
