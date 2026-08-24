"""The admin stuck-request view must show a request blocked at approval.

A conflict with an already-approved absence is raised when a manager tries to
approve, and by design nothing is written when it is: whether to reject is the
manager's call, not the system's. The cost of writing nothing is that the
request looks exactly like one nobody has opened yet. These pin the diagnostic
that tells them apart, including that it says reprocessing will not help —
reprocessing re-sends the approval email, which would hit the same wall.
"""

from app.routes.dashboard import _diagnose_stuck_leave

# The request under inspection: dates present, so only the conflict is at issue.
PENDING_FIELDS = {
    "StartDate": "2026-09-02T00:00:00Z",
    "EndDate": "2026-09-02T00:00:00Z",
    "Days": 1,
    "ManagerLookupId": 5,
}

WHOLE_DAY_CONFLICT = {
    "item_id": "11",
    "start_date": "2026-09-01",
    "end_date": "2026-09-05",
    "status": "Approved",
}

PART_DAY_CONFLICT = {
    "item_id": "11",
    "start_date": "2026-09-02",
    "end_date": "2026-09-02",
    "status": "Approved",
    "day_already_booked": 0.5,
}


def test_a_blocked_request_is_flagged_with_the_request_it_clashes_with():
    codes, detail = _diagnose_stuck_leave(
        PENDING_FIELDS, {}, {}, blocked_by=WHOLE_DAY_CONFLICT,
    )

    assert "blocked_by_overlap" in codes
    assert "#11" in detail
    assert "2026-09-01" in detail and "2026-09-05" in detail
    # The one action the view offers is Reprocess, which would not clear this.
    assert "Reprocessing will not clear this" in detail


def test_a_part_day_block_says_how_much_of_the_day_is_taken():
    codes, detail = _diagnose_stuck_leave(
        PENDING_FIELDS, {}, {}, blocked_by=PART_DAY_CONFLICT,
    )

    assert "blocked_by_overlap" in codes
    assert "0.5 day is already approved" in detail
    assert "exceed one day" in detail


def test_an_unblocked_request_carries_no_overlap_diagnostic():
    codes, _ = _diagnose_stuck_leave(PENDING_FIELDS, {}, {})
    assert "blocked_by_overlap" not in codes


def test_the_block_is_reported_alongside_whatever_else_is_wrong():
    # An empty staff lookup means the submitter cannot be resolved either; both
    # findings must survive, since the block is not the only thing to fix.
    codes, detail = _diagnose_stuck_leave(
        PENDING_FIELDS, {}, {}, blocked_by=WHOLE_DAY_CONFLICT,
    )

    assert "blocked_by_overlap" in codes
    assert "missing_employee" in codes
    assert "#11" in detail and "Staff Directory record" in detail
