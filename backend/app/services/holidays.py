import logging
from datetime import date, datetime

from app.repositories import get_holiday_repository
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.repositories.sharepoint.holidays import SharePointHolidayRepository

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
    title = (data.get("title") or "").strip()            # required, trimmed
    if not title:
        raise HolidayValidationError("A name is required.")

    # Accept a string date (from the form) or a passed-through date object.
    raw_date = (data.get("date") or "").strip() if isinstance(data.get("date"), str) else data.get("date")
    if not raw_date:
        raise HolidayValidationError("A date is required.")
    if _parse_date(raw_date) is None:                    # same parser the calculator uses
        raise HolidayValidationError("The date must be a valid date (YYYY-MM-DD).")

    province = (data.get("province") or "").strip()      # optional; blank = no province filter match

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
    fields = build_holiday_fields(data)                  # validate before any write
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
    fields = build_holiday_fields(data)                  # validate before any write
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


# --- One-time SharePoint -> Postgres copy + parity check ---------------------
# These let an admin move the Company Holidays into Postgres and confirm both
# sides agree before STORAGE_HOLIDAYS is flipped. Both talk to the two backends
# directly (not get_holiday_repository), because they must compare / fill both
# regardless of which one the flag currently selects.

# The holiday fields that carry business meaning; everything else SharePoint
# returns (system columns) is ignored by the copy and the parity comparison.
_PARITY_FIELDS = ("Title", "Date", "Province")


def _mapped_fields(item: dict) -> dict:
    """Pull only the business fields (Title, Date, Province) from an item.

    Args:
        item: A holiday in the {"id","fields"} shape.

    Returns:
        A dict of just the mapped SharePoint columns, so the Postgres write
        never sees system columns it cannot map.
    """
    fields = item.get("fields", {})
    return {name: fields.get(name) for name in _PARITY_FIELDS}


def _normalise(name: str, value):
    """Make one field comparable across the two backends.

    Args:
        name: The SharePoint column name.
        value: The raw value from either side.

    Returns:
        A date for "Date" (so "2026-07-01T00:00:00Z" equals "2026-07-01"), and
        a stripped string otherwise (so None and "" compare equal).
    """
    if name == "Date":
        return _parse_date(value)                        # both sides -> a date or None
    return (value or "").strip()                         # None/"" collapse together


async def copy_holidays_from_sharepoint() -> dict:
    """Copy every Company Holiday from SharePoint into Postgres. Idempotent.

    Reads through SharePointHolidayRepository and upserts each row into Postgres
    keyed on its SharePoint item id, so running it twice updates the same rows
    rather than duplicating them. Safe to re-run as the annual sync.

    Returns:
        A snake_case summary: source_count (rows read from SharePoint), created
        (newly inserted) and updated (already present, overwritten).
    """
    sp_repo = SharePointHolidayRepository()              # source of record today
    pg_repo = PostgresHolidayRepository()                # copy destination
    items = await sp_repo.get_all()

    created = 0                                          # rows inserted this run
    updated = 0                                          # rows overwritten this run
    for item in items:
        _, was_created = await pg_repo.upsert(item.get("id"), _mapped_fields(item))
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(
        "Holiday copy from SharePoint: %d read, %d created, %d updated",
        len(items), created, updated,
    )
    return {"source_count": len(items), "created": created, "updated": updated}


async def holidays_parity() -> dict:
    """Compare SharePoint and Postgres holidays without writing anything.

    Returns:
        A snake_case report: in_parity (True when the two sides match exactly),
        both counts, the ids present on only one side, and per-item field
        mismatches (SharePoint value vs Postgres value for Title/Date/Province).
    """
    sp_items = await SharePointHolidayRepository().get_all()
    pg_items = await PostgresHolidayRepository().get_all()

    # Index both sides by SharePoint id (the stable key across the copy).
    sp_by_id = {str(i.get("id")): i.get("fields", {}) for i in sp_items}
    pg_by_id = {str(i.get("id")): i.get("fields", {}) for i in pg_items}

    only_in_sharepoint = sorted(set(sp_by_id) - set(pg_by_id))  # not yet copied
    only_in_postgres = sorted(set(pg_by_id) - set(sp_by_id))    # deleted in SP, stale in PG

    mismatched = []                                      # rows on both sides whose fields differ
    for item_id in sorted(set(sp_by_id) & set(pg_by_id)):
        sp_fields, pg_fields = sp_by_id[item_id], pg_by_id[item_id]
        differences = {}                                 # column -> {sharepoint, postgres}
        for name in _PARITY_FIELDS:
            sp_value = _normalise(name, sp_fields.get(name))
            pg_value = _normalise(name, pg_fields.get(name))
            if sp_value != pg_value:
                differences[name] = {
                    "sharepoint": str(sp_fields.get(name)),  # show the raw values to the admin
                    "postgres": str(pg_fields.get(name)),
                }
        if differences:
            mismatched.append({"id": item_id, "differences": differences})

    in_parity = not (only_in_sharepoint or only_in_postgres or mismatched)
    return {
        "in_parity": in_parity,
        "sharepoint_count": len(sp_items),
        "postgres_count": len(pg_items),
        "only_in_sharepoint": only_in_sharepoint,
        "only_in_postgres": only_in_postgres,
        "mismatched": mismatched,
    }


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
