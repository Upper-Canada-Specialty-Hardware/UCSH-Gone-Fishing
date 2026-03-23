import logging
from datetime import date, datetime

from app.config import settings
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)


async def get_holidays_for_province(province: str) -> list[dict]:
    # Province is not indexed — fetch all and filter client-side
    items = await sp_client.get_list_items(settings.SP_LIST_COMPANY_HOLIDAYS)
    return [
        item.get("fields", {}) for item in items
        if item.get("fields", {}).get("Province", "") == province
    ]


def get_half_friday_season(holidays: list[dict]) -> tuple[date | None, date | None]:
    start_date = None
    end_date = None
    for h in holidays:
        title = h.get("Title", "")
        if "Half Fridays START" in title:
            start_date = _parse_date(h.get("Date"))
        elif "Half Fridays END" in title:
            end_date = _parse_date(h.get("Date"))
    return start_date, end_date


def is_half_friday(d: date, season: tuple[date | None, date | None]) -> bool:
    start, end = season
    if not start or not end:
        return False
    return d.weekday() == 4 and start <= d <= end  # 4 = Friday


def is_company_holiday(d: date, holidays: list[dict]) -> tuple[bool, str | None]:
    for h in holidays:
        title = h.get("Title", "")
        if "START" in title or "END" in title:
            continue
        holiday_date = _parse_date(h.get("Date"))
        if holiday_date and holiday_date == d:
            return True, title
    return False, None


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None
