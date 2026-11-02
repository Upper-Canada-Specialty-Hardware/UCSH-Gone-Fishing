"""Who goes on leave today, for the daily out-of-office email (#466).

The email lists every staff member in the listed provinces (Ontario only by
default, ``OUT_OF_OFFICE_EMAIL_PROVINCES``) whose approved leave starts today,
and the working day they are returning. Someone already away on the previous
working day is not listed again: each absence shows once, on its first day.
Every leave type counts, work from home and part days included; the leave type
and the location are never shown.

The logic is split the same way as the balance engine: pure functions that take
plain data and today's date (no I/O, so the tests pin every rule), and one async
reader, ``people_starting_leave``, that loads the requests, the staff records and
the holidays and hands them to the pure part.
"""
import logging
from datetime import date, timedelta

from app.config import settings
from app.repositories.request_store import request_store
from app.repositories.staff_store import staff_store
from app.services.employee import LOCATION_PROVINCE_MAP
from app.services.holidays import _parse_date, get_holidays_for_province, is_company_holiday

logger = logging.getLogger(__name__)

# The leave type of a part day off; it starts and ends on the same day.
PART_DAY = "Half Day or Partial Day Off"
# How far ahead to look for the next working day; a guard against bad holiday data.
MAX_LOOKAHEAD_DAYS = 400


def listed_provinces() -> set[str]:
    """The province codes whose staff are listed, from the setting.

    Returns:
        Upper-case codes, e.g. {"ON"}; blanks dropped.
    """
    return {p.strip().upper() for p in settings.OUT_OF_OFFICE_EMAIL_PROVINCES.split(",") if p.strip()}


def is_working_day(day: date, holidays: list[dict]) -> bool:
    """Whether a day is a working day: a weekday that is not a company holiday.

    Args:
        day: The day to test.
        holidays: The person's province's holiday rows (Title, Date).

    Returns:
        True on Monday to Friday unless it is a company holiday.
    """
    if day.weekday() >= 5:                                    # 5 = Saturday, 6 = Sunday
        return False
    return not is_company_holiday(day, holidays)[0]           # a holiday is not a working day


def next_working_day(after: date, holidays: list[dict]) -> date:
    """The first working day after a given day.

    Args:
        after: The last day off.
        holidays: The person's province's holiday rows.

    Returns:
        The next weekday that is not a company holiday.
    """
    day = after + timedelta(days=1)
    for _ in range(MAX_LOOKAHEAD_DAYS):                       # bounded, in case the data is odd
        if is_working_day(day, holidays):
            return day
        day += timedelta(days=1)
    return day                                                # never reached with real holidays


def previous_working_day(before: date, holidays: list[dict]) -> date:
    """The last working day before a given day.

    Args:
        before: The day to look back from.
        holidays: The person's province's holiday rows.

    Returns:
        The closest earlier weekday that is not a company holiday.
    """
    day = before - timedelta(days=1)
    for _ in range(MAX_LOOKAHEAD_DAYS):                       # bounded, in case the data is odd
        if is_working_day(day, holidays):
            return day
        day -= timedelta(days=1)
    return day                                                # never reached with real holidays


def short_day(day: date, today: date) -> str:
    """A returning date as the email shows it, e.g. "Mon, Oct 27".

    Args:
        day: The day to show.
        today: Today, to decide whether the year is needed.

    Returns:
        "Mon, Oct 27", or "Mon, Jan 4, 2027" when it is not this year.
    """
    text = f"{day:%a}, {day:%b} {day.day}"                    # day.day: no leading zero on any OS
    return text if day.year == today.year else f"{text}, {day.year}"


def day_label(today: date) -> str:
    """Today in words, under the email's heading, e.g. "Friday, October 23, 2026"."""
    return f"{today:%A}, {today:%B} {today.day}, {today.year}"


def subject_for(today: date) -> str:
    """The email's subject, e.g. "On leave starting today: Fri, Oct 23"."""
    return f"On leave starting today: {today:%a}, {today:%b} {today.day}"


def _leave_span(fields: dict) -> tuple[date, date] | None:
    """An approved leave's first and last day.

    Args:
        fields: The request's fields (StartDate, EndDate, Status).

    Returns:
        (start, end), with end = start when there is no end date; None when the
        request is not approved or has no start date.
    """
    if str(fields.get("Status") or "").strip().lower() != "approved":   # approved only
        return None
    start = _parse_date(fields.get("StartDate"))
    if start is None:
        return None
    end = _parse_date(fields.get("EndDate")) or start         # a one-day leave may carry no end
    return start, max(start, end)                             # a reversed range reads as one day


def returning_for(today: date, leaves: list[dict], holidays: list[dict]) -> str | None:
    """When one person is returning, or None when their leave does not start today.

    Listed only on the first day of an absence: today must be covered by an
    approved leave, and the previous working day must not be a full day off
    (then they were already away, e.g. the second day of a vacation, or leave
    entered from a Saturday whose first working day was Monday). A part day off
    on the previous working day does not count: they were in for some of it.

    The returning day is always a working day after today, never today itself,
    a part day off included: the day after the last leave covering today is
    moved to the next working day, and when another approved full-day leave
    covers that day too (back-to-back leave), the walk carries on from its end.

    Args:
        today: Today's date.
        leaves: The person's leaves as request fields (approved ones count).
        holidays: The holidays of the province they work in.

    Returns:
        A short returning date such as "Mon, Oct 27", or None.
    """
    spans = []                                                # (start, end, part_day)
    for fields in leaves:
        span = _leave_span(fields)
        if span:
            spans.append((span[0], span[1], fields.get("LeaveType") == PART_DAY))

    def full_day_off(day: date) -> list[date]:
        """The end dates of full-day leaves covering a day (a part day is not one)."""
        return [end for start, end, part in spans if not part and start <= day <= end]

    today_spans = [s for s in spans if s[0] <= today <= s[1]]   # leave covering today
    if not today_spans:
        return None                                           # not on leave today
    if full_day_off(previous_working_day(today, holidays)):
        return None                                           # already away: listed on the first day only

    last_off = max(end for _, end, _ in today_spans)          # the last day off; a part day ends today
    for _ in range(MAX_LOOKAHEAD_DAYS):                       # bounded walk over back-to-back leave
        returning = next_working_day(last_off, holidays)
        following = full_day_off(returning)                   # another full-day leave pushes it on
        if not following:
            return short_day(returning, today)
        last_off = max(following)
    return short_day(next_working_day(last_off, holidays), today)


def who_starts_leave(
    today: date,
    leaves: list[dict],
    staff: list[dict],
    holidays_by_province: dict[str, list[dict]],
    provinces: set[str],
) -> list[dict]:
    """Everyone in the listed provinces whose approved leave starts today, sorted by name.

    Args:
        today: Today's date.
        leaves: Every leave request as fields (EmployeeID, Status, StartDate,
            EndDate, LeaveType).
        staff: Active staff records in the {"id", "fields"} shape.
        holidays_by_province: Holiday rows per province code.
        provinces: The province codes to list, e.g. {"ON"}.

    Returns:
        One {"name", "returning"} per person, sorted by name.
    """
    by_employee: dict[str, list[dict]] = {}
    for fields in leaves:
        employee_id = str(fields.get("EmployeeID") or "").strip()
        if employee_id:                                       # an unlinked request names nobody
            by_employee.setdefault(employee_id, []).append(fields)

    people = []
    for record in staff:
        fields = record.get("fields", {})
        province = LOCATION_PROVINCE_MAP.get(fields.get("Location") or "")
        if province not in provinces:                         # BC, other provinces and unknown offices left out
            continue
        returning = returning_for(today, by_employee.get(str(record.get("id")), []), holidays_by_province.get(province, []))
        if returning:
            people.append({"name": (fields.get("Title") or "").strip(), "returning": returning})
    return sorted(people, key=lambda p: p["name"].lower())    # sorted by name, case blind


async def people_starting_leave(today: date) -> list[dict]:
    """Read the live data and list everyone whose leave starts today (see ``who_starts_leave``).

    Reads every leave request (through the request store, so it follows where
    requests live), the active staff records, and the holidays of each listed
    province.

    Args:
        today: Today's date in Toronto.

    Returns:
        One {"name", "returning"} per person, sorted by name.
    """
    provinces = listed_provinces()
    items = await request_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS)
    leaves = [item.get("fields", {}) for item in items]
    staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)   # active staff only
    holidays = {p: await get_holidays_for_province(p) for p in provinces}
    return who_starts_leave(today, leaves, staff, holidays, provinces)


async def is_holiday_everywhere(today: date) -> bool:
    """Whether today is a company holiday in every listed province.

    Args:
        today: Today's date in Toronto.

    Returns:
        True when no listed province is working today, so the email is skipped.
    """
    provinces = listed_provinces()
    if not provinces:
        return False
    for province in provinces:
        if not is_company_holiday(today, await get_holidays_for_province(province))[0]:
            return False                                      # someone is working: send
    return True
