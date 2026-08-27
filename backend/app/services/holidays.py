import logging
from datetime import date, datetime

from app.repositories import get_holiday_repository

logger = logging.getLogger(__name__)


class HolidayValidationError(ValueError):
    """Raised when a submitted holiday cannot become a working row.

    Carries a message written for the admin filling in the form, so the route
    can surface it directly rather than as a generic 500.
    """


def build_holiday_fields(data: dict) -> dict:
    """Validate an admin's holiday form and assemble the field payload. No I/O.

    Args:
        data: The submitted form, snake_case keys (title, date, province).

    Returns:
        SharePoint-shaped field dict (Title, Date, Province) for the repository.

    Raises:
        HolidayValidationError: On a missing title or an unparseable date.
    """
    title = (data.get("title") or "").strip()
    if not title:
        raise HolidayValidationError("A name is required.")

    raw_date = (data.get("date") or "").strip() if isinstance(data.get("date"), str) else data.get("date")
    if not raw_date:
        raise HolidayValidationError("A date is required.")
    if _parse_date(raw_date) is None:  # same parser the calculator uses
        raise HolidayValidationError("The date must be a valid date (YYYY-MM-DD).")

    province = (data.get("province") or "").strip()  # optional; blank = no province filter match

    return {"Title": title, "Date": str(raw_date), "Province": province}


async def list_all_holidays() -> list[dict]:
    """Every holiday row, sorted by date, in the {"id","fields"} shape.

    Returns:
        Holidays sorted by Date ascending (undated rows last) for the admin grid.
    """
    items = await get_holiday_repository().get_all()
    return sorted(
        items,
        key=lambda i: (_parse_date(i.get("fields", {}).get("Date")) or date.max),  # undated sink to the end
    )


async def create_holiday(data: dict) -> dict:
    """Validate and insert a holiday through the seam.

    Args:
        data: The submitted form (title, date, province).

    Returns:
        The created holiday as {"id","fields"}.

    Raises:
        HolidayValidationError: On invalid input.
    """
    fields = build_holiday_fields(data)
    item = await get_holiday_repository().create(fields)
    logger.info("Holiday created: #%s %s", item.get("id"), fields["Title"])
    return item


async def update_holiday(item_id: str | int, data: dict) -> dict:
    """Validate and patch a holiday through the seam.

    Args:
        item_id: The holiday to update.
        data: The submitted form (title, date, province).

    Returns:
        The updated holiday as {"id","fields"}.

    Raises:
        HolidayValidationError: On invalid input.
        KeyError: If the holiday does not exist.
    """
    fields = build_holiday_fields(data)
    item = await get_holiday_repository().update_fields(item_id, fields)
    logger.info("Holiday updated: #%s %s", item_id, fields["Title"])
    return item


async def delete_holiday(item_id: str | int) -> None:
    """Delete a holiday through the seam.

    Args:
        item_id: The holiday to remove.

    Raises:
        KeyError: If the holiday does not exist.
    """
    await get_holiday_repository().delete(item_id)
    logger.info("Holiday deleted: #%s", item_id)


async def get_holidays_for_province(province: str) -> list[dict]:
    # Province is not indexed — fetch all and filter client-side
    items = await get_holiday_repository().get_all()
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
