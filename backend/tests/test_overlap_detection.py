"""Tests for duplicate/overlap detection and its approval-time entry points.

Two behaviours are worth pinning down, because both were changed deliberately
and both are easy to regress:

  1. Only an APPROVED request reserves dates. A pending request must not block,
     which is what previously let a stalled request lock an employee out of
     those dates permanently while staying invisible on the dashboards.
  2. The approval-time helpers must never match a request against itself, and
     must let the approval through (loudly) when the submitter cannot be
     identified, rather than blocking on a lookup failure.

The list reads are faked, so nothing here touches SharePoint.
"""

import asyncio

from app.services import overlap_detection as od


def _fake_list_items(items):
    """Build a stand-in for sp_client.get_list_items that returns `items`.

    Args:
        items: The list rows to hand back, in SharePoint's {"id", "fields"} shape.

    Returns:
        An async callable accepting whatever arguments the real client takes.
    """
    async def _get_list_items(*args, **kwargs):
        return items
    return _get_list_items


def _leave(item_id, status, start, end, submitter=7):
    """One leave row in the shape Graph returns for the leave requests list."""
    return {
        "id": str(item_id),
        "fields": {
            "Status": status,
            "StartDate": start,
            "EndDate": end,
            "SubmittedTestLookupId": submitter,
        },
    }


def _overtime(item_id, status, on_date, submitter=7):
    """One overtime row; note the different person column from leave."""
    return {
        "id": str(item_id),
        "fields": {
            "Status": status,
            "StartDate": on_date,
            "SubmittedByLookupId": submitter,
        },
    }


def _leave_overlap(monkeypatch, existing, **kwargs):
    """Run check_leave_overlap against a faked list."""
    monkeypatch.setattr(od.sp_client, "get_list_items", _fake_list_items(existing))
    params = {"submitter_lookup_id": 7, "start_date": "2026-09-02", "end_date": "2026-09-04"}
    params.update(kwargs)
    return asyncio.run(od.check_leave_overlap(**params))


def _overtime_overlap(monkeypatch, existing, **kwargs):
    """Run check_overtime_overlap against a faked list."""
    monkeypatch.setattr(od.sp_client, "get_list_items", _fake_list_items(existing))
    params = {"submitter_lookup_id": 7, "overtime_date": "2026-09-02"}
    params.update(kwargs)
    return asyncio.run(od.check_overtime_overlap(**params))


# ----- only approved requests reserve dates -----

def test_only_approved_is_a_blocking_status():
    # Pinned explicitly: a pending request has not spent any balance yet, so it
    # holds no claim on the dates.
    assert od.BLOCKING_STATUSES == {"Approved"}


def test_pending_request_does_not_block(monkeypatch):
    existing = [_leave(11, "Pending", "2026-09-01", "2026-09-05")]
    assert _leave_overlap(monkeypatch, existing) is None


def test_approved_request_blocks(monkeypatch):
    existing = [_leave(11, "Approved", "2026-09-01", "2026-09-05")]
    conflict = _leave_overlap(monkeypatch, existing)
    assert conflict is not None
    assert conflict["item_id"] == "11"
    assert conflict["start_date"] == "2026-09-01"
    assert conflict["end_date"] == "2026-09-05"


def test_rejected_request_does_not_block(monkeypatch):
    # Including a request auto-rejected earlier — those must never bar a retry.
    existing = [_leave(11, "Rejected", "2026-09-01", "2026-09-05")]
    assert _leave_overlap(monkeypatch, existing) is None


def test_another_employees_approved_leave_does_not_block(monkeypatch):
    existing = [_leave(11, "Approved", "2026-09-01", "2026-09-05", submitter=99)]
    assert _leave_overlap(monkeypatch, existing) is None


def test_adjacent_dates_do_not_overlap(monkeypatch):
    # Existing leave ends the day before the new one starts.
    existing = [_leave(11, "Approved", "2026-08-28", "2026-09-01")]
    assert _leave_overlap(monkeypatch, existing, start_date="2026-09-02", end_date="2026-09-04") is None


def test_touching_dates_do_overlap(monkeypatch):
    # Shared boundary day counts as a clash — the employee cannot be off twice.
    existing = [_leave(11, "Approved", "2026-08-28", "2026-09-02")]
    conflict = _leave_overlap(monkeypatch, existing, start_date="2026-09-02", end_date="2026-09-04")
    assert conflict is not None


# ----- self-exclusion and unusable input -----

def test_request_never_blocks_itself(monkeypatch):
    existing = [_leave(11, "Approved", "2026-09-01", "2026-09-05")]
    assert _leave_overlap(monkeypatch, existing, exclude_item_id="11") is None


def test_unparseable_new_dates_return_none(monkeypatch):
    existing = [_leave(11, "Approved", "2026-09-01", "2026-09-05")]
    assert _leave_overlap(monkeypatch, existing, start_date="", end_date="") is None


def test_existing_row_with_no_dates_is_skipped(monkeypatch):
    existing = [_leave(11, "Approved", None, None)]
    assert _leave_overlap(monkeypatch, existing) is None


# ----- overtime uses the same rule on a single date -----

def test_overtime_pending_same_date_does_not_block(monkeypatch):
    existing = [_overtime(21, "Pending", "2026-09-02")]
    assert _overtime_overlap(monkeypatch, existing) is None


def test_overtime_approved_same_date_blocks(monkeypatch):
    existing = [_overtime(21, "Approved", "2026-09-02")]
    conflict = _overtime_overlap(monkeypatch, existing)
    assert conflict is not None
    assert conflict["item_id"] == "21"
    assert conflict["date"] == "2026-09-02"


def test_overtime_different_date_does_not_block(monkeypatch):
    existing = [_overtime(21, "Approved", "2026-09-03")]
    assert _overtime_overlap(monkeypatch, existing) is None


# ----- approval-time entry points -----

def test_leave_approval_conflict_names_the_approved_request(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(11, "Approved", "2026-09-01", "2026-09-05")]),
    )
    fields = {"SubmittedTestLookupId": 7, "StartDate": "2026-09-02", "EndDate": "2026-09-04"}

    message = asyncio.run(od.find_leave_approval_conflict("12", fields))

    assert message is not None
    # The manager needs the identifier and the dates to act on it.
    assert "#11" in message
    assert "2026-09-01" in message and "2026-09-05" in message


def test_leave_approval_conflict_is_none_when_only_pending_exists(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(11, "Pending", "2026-09-01", "2026-09-05")]),
    )
    fields = {"SubmittedTestLookupId": 7, "StartDate": "2026-09-02", "EndDate": "2026-09-04"}

    assert asyncio.run(od.find_leave_approval_conflict("12", fields)) is None


def test_leave_approval_conflict_excludes_the_request_being_approved(monkeypatch):
    # The request under approval is itself Approved-shaped in the list; it must
    # not be treated as its own conflict.
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(12, "Approved", "2026-09-02", "2026-09-04")]),
    )
    fields = {"SubmittedTestLookupId": 7, "StartDate": "2026-09-02", "EndDate": "2026-09-04"}

    assert asyncio.run(od.find_leave_approval_conflict("12", fields)) is None


def test_leave_approval_conflict_allows_approval_when_submitter_unknown(monkeypatch):
    # A lookup failure must not block a manager from approving; the helper logs
    # and stands aside rather than inventing a conflict.
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(11, "Approved", "2026-09-01", "2026-09-05")]),
    )
    fields = {"StartDate": "2026-09-02", "EndDate": "2026-09-04"}  # no person column at all

    assert asyncio.run(od.find_leave_approval_conflict("12", fields)) is None


def test_leave_approval_conflict_reads_the_nested_person_shape(monkeypatch):
    # SharePoint-created items carry the person field as a nested dict rather
    # than a plain lookup id; both shapes must resolve to the same employee.
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(11, "Approved", "2026-09-01", "2026-09-05")]),
    )
    fields = {
        "SubmittedTest": {"LookupId": 7},
        "StartDate": "2026-09-02",
        "EndDate": "2026-09-04",
    }

    assert asyncio.run(od.find_leave_approval_conflict("12", fields)) is not None


def test_overtime_approval_conflict_names_the_approved_entry(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_overtime(21, "Approved", "2026-09-02")]),
    )
    fields = {"SubmittedByLookupId": 7, "StartDate": "2026-09-02"}

    message = asyncio.run(od.find_overtime_approval_conflict("22", fields))

    assert message is not None
    assert "#21" in message
    assert "2026-09-02" in message


def test_overtime_approval_conflict_is_none_when_date_is_free(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_overtime(21, "Approved", "2026-09-03")]),
    )
    fields = {"SubmittedByLookupId": 7, "StartDate": "2026-09-02"}

    assert asyncio.run(od.find_overtime_approval_conflict("22", fields)) is None


# ----- the message has to survive the SMS channel -----

# One GSM-7 text. Past this the carrier splits the message and bills twice, so
# the sentence these helpers return is written to fit inside the wrapper the SMS
# handler puts around it.
SMS_SEGMENT_CHARS = 160


def _sms_text(item_id, message):
    """Reproduce the wrapper app/routes/twilio.py puts around a refusal."""
    return f"Request #{item_id} was not approved. {message}"


def test_leave_conflict_message_fits_one_text(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_leave(11, "Approved", "2026-09-01", "2026-09-05")]),
    )
    fields = {"SubmittedTestLookupId": 7, "StartDate": "2026-09-02", "EndDate": "2026-09-04"}

    message = asyncio.run(od.find_leave_approval_conflict("12", fields))

    assert len(_sms_text("12", message)) <= SMS_SEGMENT_CHARS


def test_overtime_conflict_message_fits_one_text(monkeypatch):
    monkeypatch.setattr(
        od.sp_client, "get_list_items",
        _fake_list_items([_overtime(21, "Approved", "2026-09-02")]),
    )
    fields = {"SubmittedByLookupId": 7, "StartDate": "2026-09-02"}

    message = asyncio.run(od.find_overtime_approval_conflict("22", fields))

    assert len(_sms_text("22", message)) <= SMS_SEGMENT_CHARS
