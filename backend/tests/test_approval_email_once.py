"""The manager's approval email goes out once per request version.

A request made on the request page used to email its manager twice: once as
the request page created it, and again seconds later when SharePoint's change
notice for that same new item reached the dispatcher. Each sender now claims
"approval email for version N" before sending, so the second call stops.

These tests call each sender twice with the same request and check the
emails and texts are sent once; that a reminder (a new version) still sends;
and that a failed send gives the claim back so a later call can retry.
Graph, the mailer and Twilio are all stubbed; the version and claim tables
are the local test database.
"""
import asyncio
import uuid

import pytest

from app.services import carryover_payout, leave_requests, overtime_requests


def run(coro):
    """Run one coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


def new_id() -> str:
    """A request id no other test uses, since the claim table outlives a test."""
    return str(uuid.uuid4().int % 10**9)


MANAGER = {"id": "77", "fields": {"Title": "Pat Manager", "EmailAddress": "pat@ucsh.com", "CellNumber": "+15555550101"}}
EMPLOYEE = {
    "id": "12",
    "fields": {
        "Title": "Casey Demo", "EmailAddress": "casey@ucsh.com", "Location": "Toronto Warden",
        "CurrentVacationBalance": 10, "CurrentSickDayBalance": 3, "CurrentOvertimeBalance": 1,
        "CarryOver": 0, "Payout": 0,
    },
}


class Outbox:
    """Collects every email and text a sender tries to send."""

    def __init__(self):
        self.emails: list[tuple[list[str], str]] = []   # (recipients, subject)
        self.texts: list[str] = []                      # phone numbers texted
        self.fail_next = False                          # make the next email raise once

    async def email(self, to, subject, html_body, **kwargs):
        """Stand-in for send_email / send_email_with_dashboard."""
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("mailer down")
        self.emails.append((list(to), subject))

    async def text(self, to, body):
        """Stand-in for send_sms."""
        self.texts.append(to)


@pytest.fixture
def outbox(monkeypatch):
    """Stub every outside call the three senders make; return the outbox."""
    box = Outbox()
    for module in (leave_requests, overtime_requests, carryover_payout):
        monkeypatch.setattr(module, "send_email_with_dashboard", box.email)
        monkeypatch.setattr(module, "send_sms", box.text)
    monkeypatch.setattr(overtime_requests, "send_email", box.email)
    # Email bodies are not under test; any HTML will do.
    for name in (
        "render_leave_approval_email", "render_leave_confirmation",
        "render_overtime_approval_email", "render_overtime_confirmation",
        "render_carryover_payout_approval_email",
    ):
        monkeypatch.setattr(f"app.templates_render.{name}", lambda *a, **k: "<p>body</p>")
    return box


def stub_item(monkeypatch, module, fields: dict) -> None:
    """Make request_store.get_list_item in a module return one pending item.

    Args:
        monkeypatch: pytest's monkeypatch.
        module: The service module whose request_store is stubbed.
        fields: The item's fields.
    """
    async def get_list_item(list_id, item_id):
        return {"id": str(item_id), "fields": dict(fields)}
    monkeypatch.setattr(module.request_store, "get_list_item", get_list_item)


def stub_leave(monkeypatch) -> None:
    """Point the leave sender at one employee, one manager and no conflicts."""
    stub_item(monkeypatch, leave_requests, {
        "Status": "Pending", "ManagerLookupId": "5", "LeaveType": "Vacation", "Days": 2,
        "StartDate": "2026-11-02", "EndDate": "2026-11-03",
    })

    async def employee(fields, column):
        return EMPLOYEE

    async def managers(emp):
        return [MANAGER]

    async def no_conflict(request_id, fields):
        return None
    monkeypatch.setattr(leave_requests, "resolve_request_submitter", employee)
    monkeypatch.setattr(leave_requests, "get_all_managers_for_employee", managers)
    monkeypatch.setattr(leave_requests, "find_leave_conflict_for_request", no_conflict)


def test_leave_approval_email_is_sent_once_when_asked_twice(monkeypatch, outbox):
    stub_leave(monkeypatch)
    request_id = new_id()
    run(leave_requests.send_approval_email(request_id))         # the request page's send
    run(leave_requests.send_approval_email(request_id))         # the dispatcher's, seconds later
    assert outbox.emails == [
        (["pat@ucsh.com"], "Leave Request - Casey Demo"),
        (["casey@ucsh.com"], "Leave Request Received - Casey Demo"),
    ]
    assert outbox.texts == ["+15555550101"]                      # one text too


def test_a_leave_reminder_still_sends(monkeypatch, outbox):
    stub_leave(monkeypatch)
    request_id = new_id()
    run(leave_requests.send_approval_email(request_id))
    run(leave_requests.send_approval_email(request_id, is_reminder=True))   # a new version
    subjects = [subject for _, subject in outbox.emails]
    assert subjects.count("Leave Request - Casey Demo") == 1
    assert subjects.count("Reminder: Leave Request - Casey Demo") == 1


def test_a_failed_leave_send_can_be_retried(monkeypatch, outbox):
    stub_leave(monkeypatch)
    request_id = new_id()
    outbox.fail_next = True
    with pytest.raises(RuntimeError):
        run(leave_requests.send_approval_email(request_id))     # the mailer refused
    run(leave_requests.send_approval_email(request_id))         # the retry is not blocked
    assert (["pat@ucsh.com"], "Leave Request - Casey Demo") in outbox.emails


def test_overtime_approval_email_is_sent_once_when_asked_twice(monkeypatch, outbox):
    stub_item(monkeypatch, overtime_requests, {
        "Status": "Pending", "ManagerLookupId": "5", "Hours": 4, "StartDate": "2026-11-04", "Title": "Inventory count",
    })

    async def no_holidays(province):
        return []

    async def no_conflict(request_id, fields):
        return None
    monkeypatch.setattr(overtime_requests, "get_holidays_for_province", no_holidays)
    monkeypatch.setattr(overtime_requests, "find_overtime_conflict_for_request", no_conflict)
    request_id = new_id()
    run(overtime_requests.send_approval_email(request_id, EMPLOYEE, [MANAGER]))
    run(overtime_requests.send_approval_email(request_id, EMPLOYEE, [MANAGER]))
    manager_emails = [e for e in outbox.emails if e[0] == ["pat@ucsh.com"]]
    assert len(manager_emails) == 1
    assert outbox.texts == ["+15555550101"]


def test_carryover_approval_email_is_sent_once_when_asked_twice(monkeypatch, outbox):
    stub_item(monkeypatch, carryover_payout, {
        "Status": "Pending", "EmployeeID": "12", "TypeofRequest": "Carry Over", "Days": 3,
    })

    async def employee(employee_id):
        return EMPLOYEE

    async def managers(emp):
        return [MANAGER]
    monkeypatch.setattr(carryover_payout, "get_employee_by_id", employee)
    monkeypatch.setattr(carryover_payout, "get_all_managers_for_employee", managers)
    request_id = new_id()
    run(carryover_payout.send_approval_email(request_id))
    run(carryover_payout.send_approval_email(request_id))
    assert len(outbox.emails) == 1
    assert outbox.texts == ["+15555550101"]
