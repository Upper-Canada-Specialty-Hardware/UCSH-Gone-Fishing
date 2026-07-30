"""Migration PR E: holidays can be served from Postgres instead of SharePoint.

The seam only pays off if a domain's two backends are interchangeable, so the
important test here is not "the repo returns rows" but "the holiday *service*
produces the same answers whichever backend it reads through". That parity is
what makes flipping STORAGE_HOLIDAYS safe.

Runs against the local SQLite fallback (DATABASE_URL is empty under conftest),
which is the same SQLAlchemy layer Postgres uses.
"""
import asyncio
import logging
from datetime import date

import pytest
from sqlalchemy import select

from app.database import Base, async_session, engine
from app.models.holiday import Holiday
from app.repositories import factory
from app.repositories.base import HolidayRepository
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.repositories.sharepoint.holidays import SharePointHolidayRepository
from app.services import holidays as holidays_service

# The same rows expressed both ways: as SharePoint list items, and as Holiday
# model rows. Parity is asserted between these two representations.
SP_ITEMS = [
    {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
    {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
    {"id": "3", "fields": {"Title": "Family Day", "Province": "ON", "Date": "2026-02-16"}},
    {"id": "4", "fields": {"Title": "Half Fridays START", "Province": "ON", "Date": "2026-06-01"}},
    {"id": "5", "fields": {"Title": "Half Fridays END", "Province": "ON", "Date": "2026-09-01"}},
]


class _FakeSharePointHolidayRepository(HolidayRepository):
    """Stands in for the Graph call so the SharePoint side needs no network."""

    async def get_all(self):
        return SP_ITEMS

    async def get_by_id(self, item_id):
        return next((i for i in SP_ITEMS if i["id"] == str(item_id)), None)


def _seed(items=SP_ITEMS):
    """Load the sample rows into the holidays table, replacing anything there."""
    async def inner():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_session() as session:
            for existing in (await session.execute(select(Holiday))).scalars():
                await session.delete(existing)
            await session.commit()
            for item in items:
                f = item["fields"]
                session.add(Holiday(
                    sp_item_id=item["id"],
                    title=f["Title"],
                    province=f["Province"],
                    date=date.fromisoformat(f["Date"]) if f["Date"] else None,
                ))
            await session.commit()

    asyncio.run(inner())


@pytest.fixture
def seeded():
    _seed()
    return PostgresHolidayRepository()


# --- Response shape ---------------------------------------------------------

def test_get_all_returns_the_sharepoint_item_shape(seeded):
    rows = asyncio.run(seeded.get_all())

    assert len(rows) == len(SP_ITEMS)
    assert rows[0] == {
        "id": "1",
        "fields": {"Title": "Canada Day", "Date": "2026-07-01", "Province": "ON"},
    }


def test_id_is_the_sharepoint_item_id_not_the_postgres_key(seeded):
    # Identity has to survive the cutover: anything that captured an id while
    # SharePoint was the source of record must still resolve.
    rows = asyncio.run(seeded.get_all())
    assert {r["id"] for r in rows} == {"1", "2", "3", "4", "5"}


def test_get_by_id_looks_up_by_sharepoint_id(seeded):
    row = asyncio.run(seeded.get_by_id("3"))
    assert row["fields"]["Title"] == "Family Day"


def test_get_by_id_accepts_an_int(seeded):
    assert asyncio.run(seeded.get_by_id(3))["fields"]["Title"] == "Family Day"


def test_get_by_id_returns_none_when_missing(seeded):
    assert asyncio.run(seeded.get_by_id("999")) is None


def test_a_null_date_survives_as_none():
    _seed([{"id": "9", "fields": {"Title": "Undated", "Province": "ON", "Date": None}}])
    rows = asyncio.run(PostgresHolidayRepository().get_all())
    assert rows[0]["fields"]["Date"] is None


def test_empty_table_warns_because_it_silently_changes_business_days(caplog):
    _seed([])
    with caplog.at_level(logging.WARNING):
        rows = asyncio.run(PostgresHolidayRepository().get_all())
    assert rows == []
    assert "holidays table is empty" in caplog.text


# --- Parity: the service behaves identically through either backend ---------

def test_province_filter_matches_sharepoint(monkeypatch, seeded):
    monkeypatch.setattr(
        holidays_service, "get_holiday_repository", lambda: PostgresHolidayRepository()
    )
    from_postgres = asyncio.run(holidays_service.get_holidays_for_province("ON"))

    monkeypatch.setattr(
        holidays_service, "get_holiday_repository", lambda: _FakeSharePointHolidayRepository()
    )
    from_sharepoint = asyncio.run(holidays_service.get_holidays_for_province("ON"))

    assert from_postgres == from_sharepoint
    assert [h["Title"] for h in from_postgres] == [
        "Canada Day", "Family Day", "Half Fridays START", "Half Fridays END",
    ]


def test_half_friday_season_parses_from_postgres_rows(monkeypatch, seeded):
    """Proves the ISO date strings this repo emits feed _parse_date correctly."""
    monkeypatch.setattr(
        holidays_service, "get_holiday_repository", lambda: PostgresHolidayRepository()
    )
    ontario = asyncio.run(holidays_service.get_holidays_for_province("ON"))

    season = holidays_service.get_half_friday_season(ontario)
    assert season == (date(2026, 6, 1), date(2026, 9, 1))
    # A Friday inside the season, and one outside it.
    assert holidays_service.is_half_friday(date(2026, 7, 3), season) is True
    assert holidays_service.is_half_friday(date(2026, 10, 2), season) is False


def test_company_holiday_lookup_works_off_postgres_rows(monkeypatch, seeded):
    monkeypatch.setattr(
        holidays_service, "get_holiday_repository", lambda: PostgresHolidayRepository()
    )
    ontario = asyncio.run(holidays_service.get_holidays_for_province("ON"))

    matched, name = holidays_service.is_company_holiday(date(2026, 7, 1), ontario)
    assert matched and name == "Canada Day"
    assert holidays_service.is_company_holiday(date(2026, 7, 2), ontario)[0] is False


# --- Factory wiring ---------------------------------------------------------

def test_factory_returns_postgres_when_the_flag_says_so(monkeypatch):
    monkeypatch.setattr(factory.settings, "STORAGE_HOLIDAYS", "postgres")
    assert isinstance(factory.get_holiday_repository(), PostgresHolidayRepository)


def test_factory_still_defaults_to_sharepoint(monkeypatch):
    monkeypatch.setattr(factory.settings, "STORAGE_HOLIDAYS", "sharepoint")
    assert isinstance(factory.get_holiday_repository(), SharePointHolidayRepository)


def test_factory_rejects_an_unknown_backend(monkeypatch):
    monkeypatch.setattr(factory.settings, "STORAGE_HOLIDAYS", "mysql")
    with pytest.raises(NotImplementedError) as excinfo:
        factory.get_holiday_repository()
    assert "mysql" in str(excinfo.value)


def test_requests_are_still_sharepoint_only(monkeypatch):
    # Employees gained a Postgres implementation in PR G; requests is the last
    # domain still without one, so it must keep refusing the flag.
    monkeypatch.setattr(factory.settings, "STORAGE_REQUESTS", "postgres")
    with pytest.raises(NotImplementedError):
        factory.get_leave_request_repository()
