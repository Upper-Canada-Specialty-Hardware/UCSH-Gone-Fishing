"""Dates in the Excel exports, written the way the dashboard shows them (#490).

Stored values arrive in several shapes: a plain day ("2026-04-21"), a UTC
timestamp ("2026-04-21T10:32:49Z"), a SharePoint date-only column saved as
Toronto midnight in UTC ("2026-04-21T04:00:00Z"), or a date/datetime object.
Every export writes them as plain text on the Toronto day, "Apr 21, 2026", so
a sheet reads like the dashboard and never shows a raw "T04:00:00Z".
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")
UTC = ZoneInfo("UTC")


def _parse(value) -> date | datetime | None:
    """A stored value as a date (a plain day) or an aware datetime (a moment).

    Args:
        value: A date, a datetime, an ISO string, or None.

    Returns:
        A ``date`` for a plain day, an aware ``datetime`` for a moment (naive
        ones are UTC, as stored), or None when blank or unreadable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):                            # check before date: datetime is a date
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return value                                           # already a plain day
    text = str(value).strip()
    if len(text) == 10:                                        # "YYYY-MM-DD": a day, no time to shift
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)                    # stored times are UTC
    if moment.astimezone(UTC).time() == datetime.min.time():
        return moment.astimezone(UTC).date()                   # exact UTC midnight: a day saved as such
    return moment


def toronto_day(value) -> date | None:
    """The Toronto calendar day of a stored value.

    A plain day is kept as written. A moment is moved to Toronto first, so a
    date-only SharePoint column saved as Toronto midnight (04:00Z or 05:00Z)
    lands on its own day, not the day before.

    Args:
        value: A date, a datetime, an ISO string, or None.

    Returns:
        The day, or None when blank or unreadable.
    """
    parsed = _parse(value)
    if isinstance(parsed, datetime):
        return parsed.astimezone(TORONTO).date()               # a moment: its Toronto day
    return parsed                                              # a day, or None


def _day_text(day: date) -> str:
    """One day in the house format, "Apr 21, 2026" (no zero pad on the day)."""
    return f"{day.strftime('%b')} {day.day}, {day.year}"       # %b is OS-safe; day.day drops the pad


def readable_date(value) -> str:
    """A stored date or timestamp as "Apr 21, 2026" on the Toronto day.

    Args:
        value: A date, a datetime, an ISO string, or None.

    Returns:
        The readable day, or "" when blank or unreadable.
    """
    day = toronto_day(value)
    return _day_text(day) if day else ""


def readable_datetime(value) -> str:
    """A stored moment as "Apr 21, 2026, 6:32 AM" in Toronto time.

    A plain day (no time) reads as the day alone, so a column mixing both
    never invents a midnight.

    Args:
        value: A date, a datetime, an ISO string, or None.

    Returns:
        The readable moment, the readable day, or "" when blank or unreadable.
    """
    parsed = _parse(value)
    if parsed is None:
        return ""
    if not isinstance(parsed, datetime):
        return _day_text(parsed)                               # no time to show
    local = parsed.astimezone(TORONTO)                         # Toronto wall clock
    hour = local.hour % 12 or 12                               # 12-hour clock, 0 reads 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{_day_text(local.date())}, {hour}:{local.minute:02d} {suffix}"
