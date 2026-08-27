"""The admin holiday editor must write real rows and refuse broken ones.

Covers the two layers the editor added: the Postgres repository's writes
(create / update / delete on the holidays table, including the minted-id scheme
and the unknown-field raise), and the /admin/holidays routes' validation
behaviour (400 on bad input, 404 on a missing row, 503 while processing is
disabled). The route tests fake the service, so they run without a database.

Runs against the local SQLite fallback (DATABASE_URL is empty under conftest).
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import Base, async_session, engine
from app.models.holiday import Holiday
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.services.holidays import HolidayValidationError, build_holiday_fields


def _seed(items):
    """Replace the holidays table's contents with the given {"id","fields"} items."""
    async def inner():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session() as session:
            from sqlalchemy import select
            for existing in (await session.execute(select(Holiday))).scalars():
                await session.delete(existing)
            await session.commit()
            for item in items:
                f = item["fields"]
                session.add(Holiday(
                    sp_item_id=item["id"], title=f["Title"],
                    date=None, province=f.get("Province"),
                ))
            await session.commit()
    asyncio.run(inner())


@pytest.fixture
def repo():
    _seed([
        {"id": "10", "fields": {"Title": "Canada Day", "Province": "ON"}},
        {"id": "11", "fields": {"Title": "BC Day", "Province": "BC"}},
    ])
    return PostgresHolidayRepository()


# --- Postgres repository writes ---------------------------------------------

def test_create_round_trips_and_mints_an_id_above_the_max(repo):
    created = asyncio.run(repo.create({
        "Title": "Family Day", "Date": "2027-02-15", "Province": "ON",
    }))
    # Seed ids are "10","11": the next id must be strictly greater.
    assert created["id"] == "12"
    assert created["fields"] == {"Title": "Family Day", "Date": "2027-02-15", "Province": "ON"}
    # Visible through the normal read path afterwards.
    assert asyncio.run(repo.get_by_id("12"))["fields"]["Title"] == "Family Day"


def test_update_patches_and_returns_the_new_shape(repo):
    updated = asyncio.run(repo.update_fields("10", {
        "Title": "Canada Day", "Date": "2027-07-01", "Province": "ON",
    }))
    assert updated["fields"]["Date"] == "2027-07-01"


def test_delete_removes_the_row(repo):
    asyncio.run(repo.delete("11"))
    assert asyncio.run(repo.get_by_id("11")) is None


def test_write_to_a_missing_holiday_raises(repo):
    with pytest.raises(KeyError):
        asyncio.run(repo.update_fields("999", {"Title": "X", "Date": "2027-01-01"}))
    with pytest.raises(KeyError):
        asyncio.run(repo.delete("999"))


def test_unknown_field_raises_rather_than_being_dropped(repo):
    with pytest.raises(KeyError) as excinfo:
        asyncio.run(repo.create({"Title": "X", "SomeNewColumn": 1}))
    assert "SomeNewColumn" in str(excinfo.value)


# --- Service validation (pure) ----------------------------------------------

def test_a_holiday_needs_a_title_and_a_parseable_date():
    with pytest.raises(HolidayValidationError, match="name"):
        build_holiday_fields({"title": "", "date": "2027-01-01"})
    with pytest.raises(HolidayValidationError, match="date"):
        build_holiday_fields({"title": "New Year", "date": ""})
    with pytest.raises(HolidayValidationError, match="valid date"):
        build_holiday_fields({"title": "New Year", "date": "not-a-date"})
    # Province is optional — season markers and company-wide days have none.
    assert build_holiday_fields({"title": "Half Fridays START", "date": "2027-06-01"})["Province"] == ""


# --- Routes (validation behaviour; service faked) -----------------------------

@pytest.fixture
def client(monkeypatch):
    from app.routes.dashboard import router as dashboard_router
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    app = FastAPI()
    app.include_router(dashboard_router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


def test_create_route_maps_validation_errors_to_400(client, monkeypatch):
    resp = client.post("/api/dashboard/admin/holidays", json={"title": "", "date": "2027-01-01"})
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"]


def test_update_route_maps_a_missing_row_to_404(client, monkeypatch):
    import app.services.holidays as svc

    async def _update(item_id, data):
        raise KeyError(item_id)  # repo's "does not exist" signal

    monkeypatch.setattr(svc, "update_holiday", _update)
    resp = client.patch(
        "/api/dashboard/admin/holidays/999",
        json={"title": "X", "date": "2027-01-01", "province": ""},
    )
    assert resp.status_code == 404


def test_writes_are_refused_while_processing_is_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    assert client.post(
        "/api/dashboard/admin/holidays", json={"title": "X", "date": "2027-01-01"}
    ).status_code == 503
    assert client.delete("/api/dashboard/admin/holidays/10").status_code == 503
