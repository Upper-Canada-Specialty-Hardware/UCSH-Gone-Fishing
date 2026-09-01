"""What the admin stuck-request view must list, and what it must not.

A conflict with an already-approved absence is raised when a manager tries to
approve, and by design nothing is written when it is: whether to reject is the
manager's call, not the system's. The cost of writing nothing is that the
request looks exactly like one nobody has opened yet. These pin the diagnostic
that tells them apart, including that it says reprocessing will not help —
reprocessing re-sends the approval email, which would hit the same wall.

Whether the approval email was ever sent is read from the approval-state row
that composing one writes, and passed in as `notified`. The view used to read
ApproveProcessedFlag instead, which answers a different question - it is set
when a decision is applied - so every request still waiting on its manager was
listed as stuck.
"""

import asyncio
import uuid

from app.config import settings
from app.routes import dashboard as dash
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


# ----- the approval email is judged by the send record, not the SharePoint flag -----


def test_a_request_whose_approval_email_went_out_is_not_flagged():
    codes, _ = _diagnose_stuck_leave(PENDING_FIELDS, {}, {}, notified=True)
    assert "approval_email_pending" not in codes


def test_a_manager_who_was_never_emailed_is_flagged():
    codes, detail = _diagnose_stuck_leave(PENDING_FIELDS, {}, {}, notified=False)

    assert "approval_email_pending" in codes
    assert "never sent" in detail
    assert "no record of one" in detail
    # The two actions the view offers, either of which sends it.
    assert "Reprocess or Remind" in detail


def test_an_unreadable_send_record_is_not_reported_as_never_sent():
    # Nothing was read, which is not the same as nothing was sent.
    codes, _ = _diagnose_stuck_leave(PENDING_FIELDS, {}, {}, notified=None)
    assert "approval_email_pending" not in codes


def test_a_request_with_no_manager_is_never_flagged_for_the_email():
    # There is nobody to email yet; missing_manager_lookup is that request's
    # finding, and reporting an unsent email alongside it says nothing.
    no_manager = {k: v for k, v in PENDING_FIELDS.items() if k != "ManagerLookupId"}

    for notified in (True, False, None):
        codes, _ = _diagnose_stuck_leave(no_manager, {}, {}, notified=notified)
        assert "approval_email_pending" not in codes


# ----- the view itself, against real approval-state rows -----
#
# The diagnostic is only half of it: the same rule decides whether a request is
# listed at all. These run the endpoint over three healthy pending requests,
# two of which have had their approval email composed.


def _pending_row(item_id, submitter_lookup_id, day):
    """A leave request needing nothing but a manager's decision."""
    return {
        "id": item_id,
        "fields": {
            "Status": "Pending",
            "LeaveType": "Vacation",
            "Title": "Time off",
            "StartDate": f"2026-09-{day}T00:00:00Z",
            "EndDate": f"2026-09-{day}T00:00:00Z",
            "Days": 1,
            "ManagerLookupId": 7,
            "SubmittedTestLookupId": submitter_lookup_id,
        },
    }


def _three_pending_rows():
    ids = [f"stuck-{uuid.uuid4().hex}" for _ in range(3)]
    rows = [_pending_row(item_id, 100 + i, f"0{i + 1}") for i, item_id in enumerate(ids)]
    return ids, rows


def _mock_sharepoint(monkeypatch, leave_rows):
    """Serve the leave list; every other list the view reads comes back empty."""
    async def _get_list_items(list_id, *args, **kwargs):
        return leave_rows if list_id == settings.SP_LIST_LEAVE_REQUESTS else []

    monkeypatch.setattr(dash.sp_client, "get_list_items", _get_list_items)


async def _seed_and_list(emailed_ids):
    from app.database import Base, engine
    from app.services.approval_versions import bump_and_snapshot

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Composing an approval email is what writes the row; nothing else does.
    for item_id in emailed_ids:
        await bump_and_snapshot(
            settings.SP_LIST_LEAVE_REQUESTS, item_id, {"Days": 1}, ("Days",),
        )

    return await dash.admin_stuck_requests()


def test_only_the_request_with_no_send_record_is_listed(monkeypatch):
    ids, rows = _three_pending_rows()
    _mock_sharepoint(monkeypatch, rows)

    result = asyncio.run(_seed_and_list(ids[:2]))

    assert [row["id"] for row in result["stuck"]] == [ids[2]]
    assert "approval_email_pending" in result["stuck"][0]["diagnostics"]


def test_an_unreadable_send_record_lists_none_of_them(monkeypatch):
    # No rows are seeded, so a failed read must not be taken as "never sent" -
    # that would put the whole pending list on the stuck tab.
    ids, rows = _three_pending_rows()
    _mock_sharepoint(monkeypatch, rows)

    async def _unreadable(matched):
        return None

    monkeypatch.setattr(dash, "fetch_approval_email_records", _unreadable)

    result = asyncio.run(dash.admin_stuck_requests())

    assert result["stuck"] == []
