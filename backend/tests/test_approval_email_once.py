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


def test_a_failed_text_keeps_the_claim(monkeypatch, outbox):
    stub_leave(monkeypatch)

    async def twilio_down(to, body):
        raise RuntimeError("twilio 401")
    monkeypatch.setattr(leave_requests, "send_sms", twilio_down)
    request_id = new_id()
    run(leave_requests.send_approval_email(request_id))         # email goes, text fails quietly
    run(leave_requests.send_approval_email(request_id))         # the dispatcher's call
    subjects = [subject for _, subject in outbox.emails]
    assert subjects.count("Leave Request - Casey Demo") == 1     # still one email
    assert subjects.count("Leave Request Received - Casey Demo") == 1


def test_reprocess_can_ask_for_a_fresh_email(monkeypatch, outbox):
    stub_leave(monkeypatch)
    request_id = new_id()
    run(leave_requests.send_approval_email(request_id))
    run(leave_requests.send_approval_email(request_id, new_version=True))   # an admin reprocess
    subjects = [subject for _, subject in outbox.emails]
    assert subjects.count("Leave Request - Casey Demo") == 2


def test_days_are_worked_out_before_the_manager_is_emailed(monkeypatch):
    order: list[str] = []

    async def create(list_id, fields):
        return {"id": "501", "fields": fields}

    async def calc(item_id):
        order.append("days")

    async def assign(item_id, notify=True):
        order.append("manager")                                  # this step sends the email

    async def alert(item_id):
        order.append("alert")
    monkeypatch.setattr(leave_requests.request_store, "create_list_item", create)
    monkeypatch.setattr(leave_requests, "auto_calculate_days", calc)
    monkeypatch.setattr(leave_requests, "auto_assign_manager", assign)
    monkeypatch.setattr(leave_requests, "send_bereavement_alert", alert)

    async def no_lookup(*a, **k):
        return None
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", no_lookup, raising=False)
    run(leave_requests.process_new_leave_request(
        {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"}, "casey@ucsh.com",
    ))
    assert order.index("days") < order.index("manager")


def stub_or_none(monkeypatch, module, fields: dict) -> None:
    """Make request_store.get_list_item_or_none in a module return one item.

    Args:
        monkeypatch: pytest's monkeypatch.
        module: The service module whose request_store is stubbed.
        fields: The item's fields.
    """
    async def get_or_none(list_id, item_id):
        return {"id": str(item_id), "fields": dict(fields)}
    monkeypatch.setattr(module.request_store, "get_list_item_or_none", get_or_none)


# ----- B1: an admin's create-and-approve suppresses the manager email, durably -----

LEAVE_FIELDS = {
    "Status": "Pending", "ManagerLookupId": "5", "LeaveType": "Vacation", "Days": 2,
    "StartDate": "2026-11-02", "EndDate": "2026-11-03",
}
OVERTIME_FIELDS = {
    "Status": "Pending", "ManagerLookupId": "5", "Hours": 4, "StartDate": "2026-11-04", "Title": "Inventory count",
}
CARRYOVER_FIELDS = {
    "Status": "Pending", "EmployeeID": "12", "ManagerID": "77", "ManagerLookupId": "5",
    "SystemState": "Not Processed", "TypeofRequest": "Carry Over", "Days": 1,
}


def test_admin_approved_leave_suppresses_the_manager_email(monkeypatch, outbox):
    # The notify=False path claims the approval email's version without sending.
    # The dispatcher's later send computes the same version, finds the claim and
    # sends nothing (#294 B1).
    stub_leave(monkeypatch)
    request_id = new_id()
    run(leave_requests._suppress_approval_notification(request_id, dict(LEAVE_FIELDS)))
    run(leave_requests.send_approval_email(request_id))          # the dispatcher's change-notice call
    assert outbox.emails == []                                   # nothing went out
    assert outbox.texts == []


def test_admin_failed_leave_approval_releases_and_asks_the_manager(monkeypatch, outbox):
    # When the admin's own approval then fails, the suppression is undone and the
    # manager is emailed the normal way, so nothing is left silently blocked (#294 S3).
    stub_leave(monkeypatch)
    stub_or_none(monkeypatch, leave_requests, LEAVE_FIELDS)
    request_id = new_id()
    run(leave_requests._suppress_approval_notification(request_id, dict(LEAVE_FIELDS)))
    asked = run(leave_requests.notify_manager_after_failed_admin_approval(request_id))
    assert asked is True
    subjects = [subject for _, subject in outbox.emails]
    assert "Leave Request - Casey Demo" in subjects             # the manager was asked after all


def test_recovery_does_nothing_for_a_request_that_is_no_longer_pending(monkeypatch, outbox):
    # A terminal decision (rejected, or gone) is not reopened by recovery (#294 S2/S3).
    stub_leave(monkeypatch)
    stub_or_none(monkeypatch, leave_requests, {**LEAVE_FIELDS, "Status": "Rejected"})
    request_id = new_id()
    run(leave_requests._suppress_approval_notification(request_id, dict(LEAVE_FIELDS)))
    asked = run(leave_requests.notify_manager_after_failed_admin_approval(request_id))
    assert asked is False
    assert outbox.emails == []                                   # nothing sent


def _stub_overtime(monkeypatch):
    """Point the overtime sender at one item, employee, manager and no conflicts."""
    stub_item(monkeypatch, overtime_requests, OVERTIME_FIELDS)

    async def no_holidays(province):
        return []

    async def no_conflict(request_id, fields):
        return None

    async def employee(fields, column):
        return EMPLOYEE

    async def managers(emp):
        return [MANAGER]
    monkeypatch.setattr(overtime_requests, "get_holidays_for_province", no_holidays)
    monkeypatch.setattr(overtime_requests, "find_overtime_conflict_for_request", no_conflict)
    monkeypatch.setattr(overtime_requests, "resolve_request_submitter", employee)
    monkeypatch.setattr(overtime_requests, "get_all_managers_for_employee", managers)


def test_admin_approved_overtime_suppresses_the_manager_email(monkeypatch, outbox):
    _stub_overtime(monkeypatch)
    request_id = new_id()
    run(overtime_requests._suppress_approval_notification(request_id, dict(OVERTIME_FIELDS)))
    run(overtime_requests.send_approval_email(request_id, EMPLOYEE, [MANAGER]))   # the dispatcher's call
    assert [e for e in outbox.emails if e[0] == ["pat@ucsh.com"]] == []           # no manager email
    assert outbox.texts == []


def test_admin_failed_overtime_approval_releases_and_asks_the_manager(monkeypatch, outbox):
    _stub_overtime(monkeypatch)
    stub_or_none(monkeypatch, overtime_requests, OVERTIME_FIELDS)
    request_id = new_id()
    run(overtime_requests._suppress_approval_notification(request_id, dict(OVERTIME_FIELDS)))
    asked = run(overtime_requests.notify_manager_after_failed_admin_approval(request_id))
    assert asked is True
    assert len([e for e in outbox.emails if e[0] == ["pat@ucsh.com"]]) == 1       # the manager was asked


def _stub_carryover(monkeypatch):
    """Stub the carry-over pipeline's employee, managers and writes; return the
    received-email subjects list it records into."""
    sent: list[str] = []

    async def email(to, subject, html_body, **kwargs):
        sent.append(subject)
    monkeypatch.setattr(carryover_payout, "send_email", email)
    for name in ("render_carryover_confirmation", "render_payout_confirmation"):
        monkeypatch.setattr(f"app.templates_render.{name}", lambda *a, **k: "<p>body</p>")

    async def no_write(list_id, item_id, fields):
        return fields

    async def employee(employee_id):
        return MANAGER if employee_id == "77" else EMPLOYEE

    async def managers(emp):
        return [MANAGER]
    monkeypatch.setattr(carryover_payout.request_store, "update_list_item_fields", no_write)
    monkeypatch.setattr(carryover_payout, "get_employee_by_id", employee)
    monkeypatch.setattr(carryover_payout, "get_all_managers_for_employee", managers)
    return sent


def test_admin_approved_carryover_suppresses_the_emails(monkeypatch, outbox):
    # Carry-over suppression claims both the received email and the manager email,
    # so the dispatcher's pipeline run sends neither (#294 B1).
    sent = _stub_carryover(monkeypatch)
    stub_item(monkeypatch, carryover_payout, CARRYOVER_FIELDS)
    request_id = new_id()
    run(carryover_payout._suppress_approval_notification(request_id))
    run(carryover_payout.run_approval_pipeline(request_id))      # the dispatcher's run
    assert sent == []                                           # no "Request Received" email
    assert outbox.emails == []                                  # no manager email


def test_admin_failed_carryover_approval_releases_and_runs_the_pipeline(monkeypatch, outbox):
    sent = _stub_carryover(monkeypatch)
    stub_item(monkeypatch, carryover_payout, CARRYOVER_FIELDS)
    stub_or_none(monkeypatch, carryover_payout, CARRYOVER_FIELDS)
    request_id = new_id()
    run(carryover_payout._suppress_approval_notification(request_id))
    asked = run(carryover_payout.notify_manager_after_failed_admin_approval(request_id))
    assert asked is True
    assert sent.count("Request Received for Carry Over") == 1    # received email now sent
    assert len(outbox.emails) == 1                              # and the manager email


def test_carryover_received_email_is_sent_once(monkeypatch, outbox):
    sent: list[str] = []

    async def email(to, subject, html_body, **kwargs):
        sent.append(subject)
    monkeypatch.setattr(carryover_payout, "send_email", email)
    for name in ("render_carryover_confirmation", "render_payout_confirmation"):
        monkeypatch.setattr(f"app.templates_render.{name}", lambda *a, **k: "<p>body</p>")
    # Both runs read "Not Processed": the case where the second starts before the
    # first has marked the item "Processing" (the stubbed write keeps nothing).
    stub_item(monkeypatch, carryover_payout, {
        "Status": "Pending", "EmployeeID": "12", "ManagerID": "77", "ManagerLookupId": "5",
        "SystemState": "Not Processed", "TypeofRequest": "Carry Over", "Days": 1,
    })

    async def no_write(list_id, item_id, fields):
        return fields

    async def employee(employee_id):
        return MANAGER if employee_id == "77" else EMPLOYEE

    async def managers(emp):
        return [MANAGER]
    monkeypatch.setattr(carryover_payout.request_store, "update_list_item_fields", no_write)
    monkeypatch.setattr(carryover_payout, "get_employee_by_id", employee)
    monkeypatch.setattr(carryover_payout, "get_all_managers_for_employee", managers)
    request_id = new_id()
    run(carryover_payout.run_approval_pipeline(request_id))      # the request page's run
    run(carryover_payout.run_approval_pipeline(request_id))      # the dispatcher's run
    assert sent.count("Request Received for Carry Over") == 1
    assert len(outbox.emails) == 1                               # and one manager email
