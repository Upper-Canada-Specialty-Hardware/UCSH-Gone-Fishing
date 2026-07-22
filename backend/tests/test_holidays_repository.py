"""PR C pilot: holidays service now reads through the repository seam.

Proves the rewired get_holidays_for_province pulls its rows from the holiday
repository (not sp_client directly) and still filters by province — and
demonstrates the payoff of the seam: a service can be exercised with an
in-memory repo, no SharePoint/Graph involved.
"""
import asyncio

from app.services import holidays as holidays_service
from app.repositories.base import HolidayRepository


class _FakeHolidayRepository(HolidayRepository):
    def __init__(self, items):
        self._items = items

    async def get_all(self):
        return self._items

    async def get_by_id(self, item_id):
        return next((i for i in self._items if str(i["id"]) == str(item_id)), None)


def test_get_holidays_for_province_filters_via_repo(monkeypatch):
    items = [
        {"id": "1", "fields": {"Title": "Canada Day", "Province": "ON", "Date": "2026-07-01"}},
        {"id": "2", "fields": {"Title": "BC Day", "Province": "BC", "Date": "2026-08-03"}},
        {"id": "3", "fields": {"Title": "Family Day", "Province": "ON", "Date": "2026-02-16"}},
    ]
    monkeypatch.setattr(
        holidays_service, "get_holiday_repository", lambda: _FakeHolidayRepository(items)
    )

    result = asyncio.run(holidays_service.get_holidays_for_province("ON"))

    assert [h["Title"] for h in result] == ["Canada Day", "Family Day"]
    # Return shape is unchanged — the service yields the fields dicts, not full items.
    assert all("Province" in h and h["Province"] == "ON" for h in result)
