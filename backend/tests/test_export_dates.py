"""Dates in the Excel exports read as the dashboard shows them (#490)."""
from datetime import date, datetime, timezone

import pytest

from app.services.export_dates import readable_date, readable_datetime


@pytest.mark.parametrize("stored, shown", [
    ("2026-04-21", "Apr 21, 2026"),                 # a plain day, read as written
    ("2026-04-21T04:00:00Z", "Apr 21, 2026"),       # a SharePoint day saved as Toronto midnight (summer)
    ("2026-01-13T05:00:00Z", "Jan 13, 2026"),       # the same in winter
    ("1990-03-03T00:00:00Z", "Mar 3, 1990"),        # a day saved at UTC midnight keeps its day
    ("2026-04-22T02:30:00Z", "Apr 21, 2026"),       # 10:30 PM in Toronto is still the 21st there
    ("2026-04-21T10:32:49+00:00", "Apr 21, 2026"),
    (date(2026, 11, 2), "Nov 2, 2026"),
    (datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), "Nov 2, 2026"),
    ("", ""), (None, ""), ("not a date", ""),
])
def test_readable_date(stored, shown):
    assert readable_date(stored) == shown


@pytest.mark.parametrize("stored, shown", [
    ("2026-04-21T10:32:49Z", "Apr 21, 2026, 6:32 AM"),   # EDT is UTC-4
    ("2026-01-12T21:52:04Z", "Jan 12, 2026, 4:52 PM"),   # EST is UTC-5
    ("2026-04-21T16:05:00Z", "Apr 21, 2026, 12:05 PM"),  # noon reads 12, not 0
    ("2026-04-21T04:00:00Z", "Apr 21, 2026, 12:00 AM"),  # a moment at Toronto midnight
    ("2026-04-21", "Apr 21, 2026"),                      # a plain day invents no time
    (None, ""),
])
def test_readable_datetime(stored, shown):
    assert readable_datetime(stored) == shown
