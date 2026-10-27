"""The weekday "on leave starting today" email (#466).

The pure part (services/out_of_office.py) is pinned with plain data: who is
listed (approved leave starting today, counted in working days, Ontario staff
only), the returning date (weekends, holidays and back-to-back leave skipped,
a part day returning the next working day too) and the wording. The sender (tasks/out_of_office_email.py) is pinned with
the data reads and the mail service faked and the once-a-day claim real.
"""
import asyncio
import random
from datetime import date, datetime

import pytest

from app.config import settings
from app.services import out_of_office as ooo
from app.tasks import out_of_office_email as task
from app.templates_render import render_out_of_office_daily

FRI = date(2026, 10, 23)                                       # a Friday
ON_HOLIDAYS = [{"Title": "Thanksgiving", "Date": "2026-10-12"}, {"Title": "Remembrance", "Date": "2026-10-26"}]


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def leave(employee_id, start, end=None, leave_type="Vacation", status="Approved"):
    """One leave request's fields."""
    return {"EmployeeID": employee_id, "StartDate": start, "EndDate": end or start,
            "LeaveType": leave_type, "Status": status}


def person(employee_id, name, location="Toronto Warden"):
    """One active staff record."""
    return {"id": employee_id, "fields": {"Title": name, "Location": location}}


# ----- the returning date -----

def test_returning_skips_the_weekend():
    assert ooo.returning_for(FRI, [leave("1", "2026-10-23")], []) == "Mon, Oct 26"


def test_returning_skips_a_holiday_after_the_weekend():
    assert ooo.returning_for(FRI, [leave("1", "2026-10-23")], ON_HOLIDAYS) == "Tue, Oct 27"


def test_back_to_back_leave_carries_on():
    leaves = [leave("1", "2026-10-23"), leave("1", "2026-10-26", "2026-10-30", "Work from Home")]
    assert ooo.returning_for(FRI, leaves, []) == "Mon, Nov 2"


def test_a_part_day_today_returns_the_next_working_day():
    assert ooo.returning_for(FRI, [leave("1", "2026-10-23", leave_type=ooo.PART_DAY)], []) == "Mon, Oct 26"


def test_a_part_day_before_back_to_back_leave_carries_on():
    leaves = [leave("1", "2026-10-23", leave_type=ooo.PART_DAY), leave("1", "2026-10-26", "2026-10-27")]
    assert ooo.returning_for(FRI, leaves, []) == "Wed, Oct 28"


def test_a_part_day_on_the_returning_day_still_counts_as_returning():
    leaves = [leave("1", "2026-10-23"), leave("1", "2026-10-26", leave_type=ooo.PART_DAY)]
    assert ooo.returning_for(FRI, leaves, []) == "Mon, Oct 26"


def test_the_year_is_shown_when_returning_next_year():
    assert ooo.returning_for(date(2026, 12, 31), [leave("1", "2026-12-31")], []) == "Fri, Jan 1, 2027"


@pytest.mark.parametrize("fields", [
    leave("1", "2026-10-22"),                                  # ended yesterday
    leave("1", "2026-10-26"),                                  # starts next week
    leave("1", "2026-10-23", status="Pending"),                # not approved
    leave("1", "2026-10-23", status="Rejected"),
    leave("1", "2026-10-23", status="Cancelled"),
])
def test_only_approved_leave_covering_today_counts(fields):
    assert ooo.returning_for(FRI, [fields], []) is None


# ----- listed on the first day of the leave only -----

def test_the_second_day_of_a_leave_is_not_listed():
    assert ooo.returning_for(FRI, [leave("1", "2026-10-22", "2026-10-23")], []) is None


def test_back_to_back_leave_over_a_weekend_is_not_listed_again():
    leaves = [leave("1", "2026-10-23"), leave("1", "2026-10-26", leave_type="Work from Home")]
    assert ooo.returning_for(date(2026, 10, 26), leaves, []) is None


def test_leave_entered_from_a_saturday_is_listed_on_monday():
    assert ooo.returning_for(date(2026, 10, 26), [leave("1", "2026-10-24", "2026-10-28")], []) == "Thu, Oct 29"


def test_leave_entered_from_a_holiday_is_listed_the_next_working_day():
    # Monday Oct 26 is a holiday here, so Tuesday is the leave's first working day.
    away = [leave("1", "2026-10-26", "2026-10-27")]
    assert ooo.returning_for(date(2026, 10, 27), away, ON_HOLIDAYS) == "Wed, Oct 28"


def test_a_part_day_the_day_before_does_not_stop_the_listing():
    leaves = [leave("1", "2026-10-22", leave_type=ooo.PART_DAY), leave("1", "2026-10-23")]
    assert ooo.returning_for(FRI, leaves, []) == "Mon, Oct 26"


def test_a_part_day_after_a_full_day_off_is_not_listed():
    leaves = [leave("1", "2026-10-22"), leave("1", "2026-10-23", leave_type=ooo.PART_DAY)]
    assert ooo.returning_for(FRI, leaves, []) is None


def test_unapproved_leave_the_day_before_does_not_count_as_away():
    leaves = [leave("1", "2026-10-22", status="Rejected"), leave("1", "2026-10-23")]
    assert ooo.returning_for(FRI, leaves, []) == "Mon, Oct 26"


# ----- who is listed -----

def test_ontario_staff_only_sorted_by_name():
    staff = [
        person("1", "Sam Whitfield"),
        person("2", "alex Moreau", "Ottawa"),
        person("3", "Bea Coast", "British Columbia"),              # BC: left out
        person("4", "Nora Nowhere", "Somewhere New"),              # unknown office: left out
        person("5", "In Today"),                                   # no leave: left out
    ]
    leaves = [leave(i, "2026-10-23") for i in ("1", "2", "3", "4")] + [leave("9", "2026-10-23")]
    listed = ooo.who_starts_leave(FRI, leaves, staff, {"ON": []}, {"ON"})
    assert listed == [{"name": "alex Moreau", "returning": "Mon, Oct 26"},
                      {"name": "Sam Whitfield", "returning": "Mon, Oct 26"}]


def test_the_province_holidays_decide_the_returning_date():
    listed = ooo.who_starts_leave(FRI, [leave("1", "2026-10-23")], [person("1", "Sam")], {"ON": ON_HOLIDAYS}, {"ON"})
    assert listed == [{"name": "Sam", "returning": "Tue, Oct 27"}]


def test_the_province_setting_is_read(monkeypatch):
    monkeypatch.setattr(settings, "OUT_OF_OFFICE_EMAIL_PROVINCES", " on, ")
    assert ooo.listed_provinces() == {"ON"}


# ----- the wording -----

def test_subject_and_day_label():
    assert ooo.subject_for(FRI) == "On leave starting today: Fri, Oct 23"
    assert ooo.day_label(FRI) == "Friday, October 23, 2026"


def test_the_email_lists_each_person_and_counts_them():
    html = render_out_of_office_daily("Friday, October 23, 2026", [{"name": "Sam <b>", "returning": "Mon, Oct 26"}])
    assert "1 person starts approved leave today." in html and ">Returning</th>" in html
    assert "Sam &lt;b&gt;" in html and "Mon, Oct 26" in html    # names are escaped


# ----- the sender -----

@pytest.mark.parametrize("now, due", [
    (datetime(2026, 10, 23, 6, 59), False),
    (datetime(2026, 10, 23, 7, 0), True),
    (datetime(2026, 10, 23, 9, 59), True),
    (datetime(2026, 10, 23, 10, 0), False),                    # a late restart sends nothing
    (datetime(2026, 10, 24, 7, 30), False),                    # Saturday
])
def test_the_send_window(now, due):
    assert task.due_now(now) is due


@pytest.fixture
def sender(monkeypatch):
    """Configured to send, with the reads and the mail service faked.

    Yields:
        A dict: "sent" (each send's to and subject) and "holiday" (set True to
        make today a holiday).
    """
    world = {"sent": [], "holiday": False, "starting": [{"name": "Sam Whitfield", "returning": "Mon, Oct 26"}]}

    async def starting(today):
        return world["starting"]

    async def holiday(today):
        return world["holiday"]

    async def send(to, subject, html_body, idempotency_key=None):
        world["sent"].append((to, subject))

    monkeypatch.setattr(settings, "OUT_OF_OFFICE_EMAIL_TO", "out-of-office@ucsh.com")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(task, "people_starting_leave", starting)
    monkeypatch.setattr(task, "is_holiday_everywhere", holiday)
    monkeypatch.setattr(task, "send_email_via_mailer", send)
    return world


def _a_day() -> date:
    """A far-off day no other run has claimed (the claim log outlives the test database wipe)."""
    return date.fromordinal(random.randint(date(3000, 1, 1).toordinal(), date(9000, 1, 1).toordinal()))


def test_sent_once_a_day_even_when_asked_twice(sender):
    day = _a_day()
    assert run(task.send_daily_email(day)) is True
    assert run(task.send_daily_email(day)) is False            # a second server, or the next minute
    assert sender["sent"] == [(["out-of-office@ucsh.com"], ooo.subject_for(day))]


def test_not_sent_without_an_address(sender, monkeypatch):
    monkeypatch.setattr(settings, "OUT_OF_OFFICE_EMAIL_TO", " ")
    assert run(task.send_daily_email(_a_day())) is False and sender["sent"] == []


def test_not_sent_while_processing_is_off(sender, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    assert run(task.send_daily_email(_a_day())) is False and sender["sent"] == []


def test_not_sent_on_a_holiday(sender):
    sender["holiday"] = True
    assert run(task.send_daily_email(_a_day())) is False and sender["sent"] == []


def test_not_sent_when_nobody_starts_leave(sender):
    sender["starting"] = []
    day = _a_day()
    assert run(task.send_daily_email(day)) is False and sender["sent"] == []
    sender["starting"] = [{"name": "Sam Whitfield", "returning": "Mon, Oct 26"}]
    assert run(task.send_daily_email(day)) is True             # the empty check did not claim the day


# ----- HVE only -----

@pytest.fixture
def mailer(monkeypatch):
    """The UCSH mailer switched on and configured, with its HTTP call faked.

    Yields:
        The list of (to, subject, key) the mailer was asked to send.
    """
    from app.graph import email as email_module
    calls = []

    async def send(to, cc, subject, html, key, reply_to=None):
        calls.append((to, subject, key))
        return "accepted"

    async def no_smtp2go(*args, **kwargs):
        raise AssertionError("SMTP2GO must never be used for this email")

    monkeypatch.setattr(settings, "MAILER_ENABLED", True)
    monkeypatch.setattr(settings, "MAILER_URL", "https://mailer.test")
    monkeypatch.setattr(settings, "MAILER_KEY", "test-key")
    monkeypatch.setattr(email_module.mailer_client, "send", send)
    monkeypatch.setattr(email_module, "_send_via_smtp2go", no_smtp2go)
    return calls


def test_the_group_email_goes_through_the_mailer(mailer):
    from app.graph.email import send_email_via_mailer
    run(send_email_via_mailer(["Out-Of-Office-Notification@ucsh.com"], "s", "<p>x</p>", idempotency_key="k"))
    assert mailer == [(["Out-Of-Office-Notification@ucsh.com"], "s", "k")]


def test_no_smtp2go_fallback_when_the_mailer_is_off(mailer, monkeypatch):
    from app.graph.email import MailerUnavailableError, send_email_via_mailer
    monkeypatch.setattr(settings, "MAILER_ENABLED", False)
    with pytest.raises(MailerUnavailableError):
        run(send_email_via_mailer(["Out-Of-Office-Notification@ucsh.com"], "s", "<p>x</p>"))
    assert mailer == []


def test_an_outside_address_is_refused(mailer):
    from app.graph.email import MailerUnavailableError, send_email_via_mailer
    with pytest.raises(MailerUnavailableError, match="Not a company address"):
        run(send_email_via_mailer(["someone@gmail.com"], "s", "<p>x</p>"))
    assert mailer == []
