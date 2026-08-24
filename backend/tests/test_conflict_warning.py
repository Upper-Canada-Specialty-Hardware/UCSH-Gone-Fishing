"""The employee must be told when their request clashes with approved leave.

Before the duplicate check moved to approval time, a clashing request was
rejected outright and the employee got an email saying why. Moving the check
downstream kept the manager informed - they see the refusal when they try to
approve - but left the employee with nothing at all: a confirmation at
submission and then silence, while the request sat pending forever.

What is carried forward from the old behaviour is the notification. What is not
is the rejection: nothing is written, nothing is cancelled, and the decision
stays with the manager. These pin both halves of that, and check the wording
each audience gets, since the same fact calls for a different next step.
"""

import asyncio

from app.services import notify_blocked as nb
from app.services.overlap_detection import (
    conflict_warning,
    find_conflict_for_row,
    find_requests_blocked_by,
)
from app.templates_render import (
    render_leave_approval_email,
    render_leave_confirmation,
    render_overtime_confirmation,
    render_request_now_blocked,
)

WHOLE_DAY = {
    "item_id": "11",
    "start_date": "2026-09-01",
    "end_date": "2026-09-05",
    "status": "Approved",
}

PART_DAY = {
    "item_id": "11",
    "start_date": "2026-09-02",
    "end_date": "2026-09-02",
    "status": "Approved",
    "day_already_booked": 0.5,
}

OVERTIME = {"item_id": "21", "date": "2026-09-02", "status": "Approved"}

FIELDS = {"LeaveType": "Vacation", "StartDate": "2026-09-02", "EndDate": "2026-09-02", "Days": 1}
EMP_FIELDS = {"CurrentVacationBalance": 10, "CurrentSickDayBalance": 5}


# ----- no conflict means no warning at all -----

def test_no_conflict_produces_no_warning():
    assert conflict_warning(None, "leave", "manager") is None
    assert conflict_warning(None, "leave", "employee") is None


def test_a_clean_confirmation_carries_no_warning_markup():
    html = render_leave_confirmation(FIELDS, EMP_FIELDS, None)
    assert "cannot be approved" not in html
    assert "already have approved" not in html


# ----- the manager is told what to do; the employee is not -----

def test_the_manager_is_given_the_two_actions_available():
    warning = conflict_warning(WHOLE_DAY, "leave", "manager")

    assert "#11" in warning["detail"]
    assert "2026-09-01" in warning["detail"] and "2026-09-05" in warning["detail"]
    # Reject or cancel the other one — the system does neither on its own.
    assert "Reject this request" in warning["action"]
    assert "cancel #11" in warning["action"]


def test_the_employee_is_told_what_happened_not_what_to_do():
    warning = conflict_warning(WHOLE_DAY, "leave", "employee")

    assert "#11" in warning["detail"]
    # Their request is still live; nothing was cancelled on their behalf.
    assert "still gone to your manager" in warning["action"]
    assert "Reject this request" not in warning["action"]


def test_both_audiences_are_given_the_same_fact():
    manager = conflict_warning(WHOLE_DAY, "leave", "manager")
    employee = conflict_warning(WHOLE_DAY, "leave", "employee")
    assert manager["detail"] == employee["detail"]


def test_a_part_day_clash_says_how_much_of_the_day_is_taken():
    warning = conflict_warning(PART_DAY, "leave", "employee")
    assert "0.5 day of 2026-09-02" in warning["detail"]
    assert "will not fit" in warning["detail"]


def test_overtime_is_worded_for_its_own_list():
    warning = conflict_warning(OVERTIME, "overtime", "manager")
    assert "Overtime request #21" in warning["detail"]
    assert "2026-09-02" in warning["detail"]


# ----- the templates actually render it -----
#
# The warning is pulled in by {% include %}, which fails silently in the sense
# that a wrong filename simply renders nothing. These prove the block reaches
# the reader in every email that carries it.

def test_the_employee_confirmation_renders_the_warning():
    warning = conflict_warning(WHOLE_DAY, "leave", "employee")
    html = render_leave_confirmation(FIELDS, EMP_FIELDS, None, conflict_warning=warning)

    assert warning["heading"] in html
    assert warning["detail"] in html
    assert "still gone to your manager" in html


def test_the_manager_approval_email_renders_the_warning():
    warning = conflict_warning(WHOLE_DAY, "leave", "manager")
    html = render_leave_approval_email(
        FIELDS, EMP_FIELDS, "https://approve", "https://reject", "Someone", None,
        conflict_warning=warning,
    )

    assert "cannot be approved yet" in html
    assert "cancel #11" in html


def test_the_overtime_confirmation_renders_the_warning():
    warning = conflict_warning(OVERTIME, "overtime", "employee")
    html = render_overtime_confirmation(FIELDS, EMP_FIELDS, None, conflict_warning=warning)

    assert "Overtime request #21" in html


# ----- an approval can strand another request the employee already had in -----
#
# This is the common shape, and the one a warning at submission cannot catch:
# both requests are submitted while nothing is approved, so neither blocks the
# other. The moment the first is approved the second stops being approvable,
# with nothing written on it and nothing said to anyone.

def _leave_row(item_id, status, start, end, submitter=7, days=1.0):
    return {
        "id": str(item_id),
        "fields": {
            "Status": status, "StartDate": start, "EndDate": end,
            "Days": days, "SubmittedTestLookupId": submitter,
        },
    }


def _stranded(items, approved_id="11", submitter=7):
    return find_requests_blocked_by(
        items, approved_id, submitter, "SubmittedTest", find_conflict_for_row,
    )


def test_the_request_left_behind_is_found():
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-09-03", "2026-09-04"),
    ]
    blocked = _stranded(items)

    assert [item["id"] for item, _ in blocked] == ["12"]
    assert blocked[0][1]["item_id"] == "11"


def test_a_request_blocked_by_something_else_is_not_reported():
    # Already blocked by #9, so it was reported when #9 was approved. Repeating
    # it on every unrelated approval would train people to ignore the message.
    items = [
        _leave_row(9, "Approved", "2026-10-01", "2026-10-02"),
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-10-01", "2026-10-02"),
    ]
    assert _stranded(items) == []


def test_another_employees_request_is_not_reported():
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-09-03", "2026-09-04", submitter=99),
    ]
    assert _stranded(items) == []


def test_an_already_actioned_request_is_not_reported():
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Rejected", "2026-09-03", "2026-09-04"),
    ]
    assert _stranded(items) == []


def test_the_approved_request_never_reports_itself():
    assert _stranded([_leave_row(11, "Approved", "2026-09-01", "2026-09-05")]) == []


def test_a_part_day_that_still_fits_is_not_stranded():
    # Half a day approved leaves half free, so the pending half is still fine.
    items = [
        _leave_row(11, "Approved", "2026-09-02", "2026-09-02", days=0.5),
        _leave_row(12, "Pending", "2026-09-02", "2026-09-02", days=0.5),
    ]
    assert _stranded(items) == []


def test_the_notice_email_names_the_request_and_the_clash():
    warning = conflict_warning(WHOLE_DAY, "leave", "employee")
    html = render_request_now_blocked("leave", "12", FIELDS, warning)

    assert "#12" in html
    assert "#11" in warning["detail"] and warning["detail"] in html
    # Their request is untouched — that is the whole point of saying nothing.
    assert "Nothing has been cancelled" in html


# ----- the notifier itself -----

def _run_notify(monkeypatch, items, fields, email="someone@ucsh.ca"):
    """Drive the notifier against faked rows, capturing what it would send."""
    sent = []

    async def _get_list_items(*args, **kwargs):
        return items

    async def _send_email(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(nb.sp_client, "get_list_items", _get_list_items)
    monkeypatch.setattr(nb, "send_email", _send_email)
    count = asyncio.run(
        nb.notify_requests_blocked_by_approval("leave", "11", fields, email)
    )
    return count, sent


APPROVED_FIELDS = {"SubmittedTestLookupId": 7}


def test_the_employee_is_emailed_once_per_stranded_request(monkeypatch):
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-09-03", "2026-09-04"),
        _leave_row(13, "Pending", "2026-09-05", "2026-09-06"),
    ]
    count, sent = _run_notify(monkeypatch, items, APPROVED_FIELDS)

    assert count == 2
    assert {s["to"][0] for s in sent} == {"someone@ucsh.ca"}
    assert "#12" in sent[0]["subject"] and "Cannot Be Approved" in sent[0]["subject"]


def test_nothing_is_sent_when_nothing_was_stranded(monkeypatch):
    items = [_leave_row(11, "Approved", "2026-09-01", "2026-09-05")]
    count, sent = _run_notify(monkeypatch, items, APPROVED_FIELDS)

    assert count == 0
    assert sent == []


def test_an_employee_with_no_email_is_skipped(monkeypatch):
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-09-03", "2026-09-04"),
    ]
    count, sent = _run_notify(monkeypatch, items, APPROVED_FIELDS, email="")

    assert count == 0
    assert sent == []


def test_an_unidentifiable_submitter_is_skipped(monkeypatch):
    items = [
        _leave_row(11, "Approved", "2026-09-01", "2026-09-05"),
        _leave_row(12, "Pending", "2026-09-03", "2026-09-04"),
    ]
    count, sent = _run_notify(monkeypatch, items, {}, email="someone@ucsh.ca")

    assert count == 0
    assert sent == []


def test_an_unreadable_list_loses_the_notice_not_the_approval(monkeypatch):
    # This runs after the approval is written, so it must never raise.
    async def _boom(*args, **kwargs):
        raise RuntimeError("Graph is down")

    monkeypatch.setattr(nb.sp_client, "get_list_items", _boom)
    count = asyncio.run(
        nb.notify_requests_blocked_by_approval("leave", "11", APPROVED_FIELDS, "someone@ucsh.ca")
    )
    assert count == 0
