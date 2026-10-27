"""Refusing a new leave request that clashes with a live one (#446).

The product-owner ruling (2026-10-26) is that a new leave request sharing any
date with one of the same person's leave requests that is not yet final -
Pending as well as Approved - is refused the moment it is submitted, before any
row is created. The person is told to cancel or change the existing one instead.

Two layers are pinned here:

  1. The pure matcher ``find_new_leave_clash`` and the refusal sentence
     ``new_leave_clash_refusal`` - the date, status and person logic, with no
     I/O. Unlike the approval-time check, Pending blocks, and part days do not
     accumulate: one request per date, so two part days on one date clash.
  2. ``request_intake.submit_request`` raising ``RequestFormError`` on a clash
     and creating nothing, which is how every in-house path (the request page,
     the admin New request screen) refuses it.

The list reads are faked, so nothing here touches SharePoint.
"""

import asyncio
from datetime import date

import pytest

from app.graph import sharepoint
from app.services import request_intake
from app.services.overlap_detection import find_new_leave_clash, new_leave_clash_refusal
from app.services.request_intake import RequestFormError, parse_request_form, submit_request


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


def _row(item_id, status, start, end, *, email="pat@ucsh.com", employee_id="7", days=1.0):
    """One leave row in the {"id", "fields"} shape the request store returns.

    Carries both the SubmitterEmail and EmployeeID an in-house row has, since
    the creation-time check matches on those (never a Microsoft 365 lookup id).
    """
    return {
        "id": str(item_id),
        "fields": {
            "Status": status,                                  # "Pending" / "Approved" / ...
            "StartDate": start,                                # ISO first day
            "EndDate": end,                                    # ISO last day
            "Days": days,                                      # a fraction marks a part day
            "SubmitterEmail": email,                           # the request page / admin identifier
            "EmployeeID": employee_id,                         # the staff record identifier
        },
    }


def _clash(items, start, end, *, email="pat@ucsh.com", employee_id="7"):
    """Run the pure matcher as the submission path calls it (lookup id None)."""
    return find_new_leave_clash(
        items,
        submitter_lookup_id=None,
        start_date=start,
        end_date=end,
        submitter_email=email,
        employee_id=employee_id,
    )


# --- the pure matcher: which rows reserve a date ---

def test_a_pending_request_on_the_same_day_clashes():
    # Pending blocks at creation, unlike the approval-time check.
    existing = [_row(3926, "Pending", "2026-10-26", "2026-10-26")]
    conflict = _clash(existing, "2026-10-26", "2026-10-26")
    assert conflict and conflict["item_id"] == "3926" and conflict["status"] == "Pending"


def test_an_approved_request_on_the_same_day_clashes():
    existing = [_row(3927, "Approved", "2026-10-26", "2026-10-28")]
    conflict = _clash(existing, "2026-10-27", "2026-10-27")     # inside the approved range
    assert conflict and conflict["item_id"] == "3927" and conflict["status"] == "Approved"


def test_a_rejected_request_does_not_clash():
    existing = [_row(3928, "Rejected", "2026-10-26", "2026-10-26")]
    assert _clash(existing, "2026-10-26", "2026-10-26") is None


def test_a_cancelled_request_does_not_clash():
    existing = [_row(3929, "Cancelled", "2026-10-26", "2026-10-26")]
    assert _clash(existing, "2026-10-26", "2026-10-26") is None


def test_two_part_days_on_one_date_clash():
    # The approval-time check lets part days share a date up to a full day; the
    # creation-time rule does not. Two half days on one date still clash (#446).
    existing = [_row(3930, "Pending", "2026-10-26", "2026-10-26", days=0.5)]
    conflict = _clash(existing, "2026-10-26", "2026-10-26")
    assert conflict and conflict["item_id"] == "3930"


def test_a_different_person_does_not_clash():
    # A row whose email and staff id both differ belongs to somebody else.
    existing = [_row(3931, "Pending", "2026-10-26", "2026-10-26",
                     email="other@ucsh.com", employee_id="99")]
    assert _clash(existing, "2026-10-26", "2026-10-26") is None


def test_adjacent_dates_do_not_clash():
    # Oct 26-27 and Oct 28-29 share no date, so the later request is free.
    existing = [_row(3932, "Approved", "2026-10-26", "2026-10-27")]
    assert _clash(existing, "2026-10-28", "2026-10-29") is None


def test_a_touching_range_clashes():
    # Ranges that meet on one date (Oct 27) overlap inclusively.
    existing = [_row(3933, "Approved", "2026-10-25", "2026-10-27")]
    conflict = _clash(existing, "2026-10-27", "2026-10-29")
    assert conflict and conflict["item_id"] == "3933"


def test_an_undated_existing_row_is_skipped():
    existing = [_row(3934, "Pending", "", "")]
    assert _clash(existing, "2026-10-26", "2026-10-26") is None


# --- the refusal sentence the person sees ---

def test_the_refusal_names_the_request_and_its_state():
    existing = [_row(3926, "Pending", "2026-10-26", "2026-10-26")]
    conflict = _clash(existing, "2026-10-26", "2026-10-26")
    assert new_leave_clash_refusal(conflict) == (
        "You already have a pending request for Oct 26, 2026 "
        "(#3926). Change that request or cancel it instead."
    )


def test_the_refusal_describes_a_range():
    existing = [_row(3927, "Approved", "2026-10-26", "2026-10-30")]
    conflict = _clash(existing, "2026-10-28", "2026-10-28")
    assert new_leave_clash_refusal(conflict) == (
        "You already have approved time off for Oct 26, 2026 to Oct 30, 2026 "
        "(#3927). To change approved time off, ask your manager or an admin."
    )


# --- submit_request: refused before any row is created ---

def _employee(**over):
    """A salaried staff record for submit_request; email and id match _row."""
    base = {"Title": "Pat Staff", "EmailAddress": "pat@ucsh.com", "SalaryHourly": "Salary",
            "CurrentVacationBalance": 10, "CarryOver": 0}
    base.update(over)
    return {"id": "7", "fields": base}


def _sick_form(start, end):
    """A Sick or Personal Day leave form (not subject to the booking limit)."""
    return parse_request_form("leave", {
        "leave_type": "Sick or Personal Day", "start_date": start, "end_date": end,
    })


def _fake_list_items(items):
    """An async stand-in for the client's get_list_items that returns `items`."""
    async def _get_list_items(*args, **kwargs):
        return items
    return _get_list_items


def test_submit_refuses_a_clashing_leave_and_creates_nothing(monkeypatch):
    # A pending request already holds Oct 26; a new leave on that date is refused.
    created = []

    async def must_not_create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", must_not_create)
    monkeypatch.setattr(
        sharepoint.sp_client, "get_list_items",
        _fake_list_items([_row(3926, "Pending", "2026-10-26", "2026-10-26")]),
    )
    form = _sick_form("2026-10-26", "2026-10-26")
    with pytest.raises(RequestFormError) as exc:
        run(submit_request("leave", form, _employee(), "request_page"))
    assert "#3926" in str(exc.value) and "pending" in str(exc.value)
    assert created == []                                       # refused before any write


def test_submit_allows_a_leave_with_no_clash(monkeypatch):
    # An empty leave list means no clash, so the request is created as usual.
    created = []

    async def create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", create)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", _fake_list_items([]))
    form = _sick_form("2026-10-26", "2026-10-26")
    item = run(submit_request("leave", form, _employee(), "request_page"))
    assert item["id"] == "99" and len(created) == 1            # it was created


def test_submit_allows_a_clash_that_belongs_to_someone_else(monkeypatch):
    # The live row is another person's, so the new request goes through.
    created = []

    async def create(*a, **k):
        created.append(a)
        return {"id": "99"}

    monkeypatch.setattr(request_intake, "process_new_leave_request", create)
    monkeypatch.setattr(
        sharepoint.sp_client, "get_list_items",
        _fake_list_items([_row(3926, "Pending", "2026-10-26", "2026-10-26",
                               email="other@ucsh.com", employee_id="99")]),
    )
    form = _sick_form("2026-10-26", "2026-10-26")
    item = run(submit_request("leave", form, _employee(), "request_page"))
    assert item["id"] == "99" and len(created) == 1


def test_overtime_submission_is_not_affected_by_the_leave_clash(monkeypatch):
    # The clash check is leave-only: an overtime submission never reads the leave
    # list, so an overlapping leave row does not block overtime (#446).
    created = []

    async def create_overtime(*a, **k):
        created.append(a)
        return {"id": "77"}

    reads = []

    async def record_reads(*a, **k):
        reads.append(a)
        return []

    monkeypatch.setattr(request_intake, "process_new_overtime_request", create_overtime)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", record_reads)
    form = parse_request_form("overtime", {
        "description": "late install", "date": date.today().isoformat(), "hours": 2,
    })
    item = run(submit_request("overtime", form, _employee(), "request_page"))
    assert item["id"] == "77" and len(created) == 1
    assert reads == []                                         # no clash read for overtime
