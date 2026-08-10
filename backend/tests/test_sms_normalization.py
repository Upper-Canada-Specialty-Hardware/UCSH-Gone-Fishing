"""Phone normalization + per-manager send resilience.

Two defects are covered here. First, `send_sms` took the last ten *characters*
of whatever SharePoint stored, so any trailing punctuation or whitespace
shifted the digits and produced an invalid number - including the trailing
space the slice was originally written to absorb. Second, the resulting Twilio
4xx escaped the per-manager loop in `send_approval_email`, so one bad recipient
cost every manager after them BOTH their email and their text, plus the
submitter's confirmation email at the end of the function.

Fixtures use non-routable values only: `.invalid` domains (reserved by RFC 2606)
and numbers in the 555-0100..0199 range (reserved for fictional use), so nothing
here can reach a real person if a test is ever pointed at a live provider.
"""
import asyncio
import logging

import pytest

from app.services.employee import LOCATION_PROVINCE_MAP
from app.services.notifications import NotificationsFailed
from app.services.sms import normalize_phone

#: A fictional number, used to build every format variant below.
_DIGITS = "4165550101"
_E164 = f"+1{_DIGITS}"


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Formats that already worked.
        (_DIGITS, _E164),
        (f"1{_DIGITS}", _E164),
        (_E164, _E164),
        (f" {_DIGITS}", _E164),                  # leading space: absorbed before too
        # Formats the old slice corrupted.
        (f"{_DIGITS} ", _E164),                  # trailing space - the original motivation
        (f"{_DIGITS}\t", _E164),
        ("416-555-0101", _E164),
        ("(416) 555-0101", _E164),
        ("416.555.0101", _E164),
        ("416 555 0101", _E164),
        ("1 (416) 555-0101", _E164),
        # Already-international numbers pass through with their own country code.
        # The national part varies in length outside North America, so a plausible
        # E.164 number must not be rejected for being shorter than a NANP one.
        ("+442071234567", "+442071234567"),
        ("+4791234567", "+4791234567"),
    ],
)
def test_normalize_phone_accepts_every_format_staff_actually_type(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",                  # whitespace-only, which is truthy in the caller's `if cell` guard
        f"{_DIGITS}x22",        # extension: the digits no longer form a valid number
        "+1 (416) 555-0101 x22",    # same, but leading "+" — must refuse alike
        f"+1 {_DIGITS} ext. 4",
        f"{_DIGITS},,99",
        f"{_DIGITS} #5",
        "555",
        "not a phone",
        "+1",                   # a country code and nothing to dial
    ],
)
def test_normalize_phone_refuses_to_guess(raw):
    """Unusable input returns None so the caller skips.

    Truncating to ten digits would text a stranger, which is worse than not
    sending at all.
    """
    assert normalize_phone(raw) is None


def test_old_slice_behaviour_is_gone():
    """Regression pin for the exact defect: last-10-characters vs last-10-digits."""
    raw = "(416) 555-0101"
    assert raw[-10:] == ") 555-0101"              # what the old code sent to Twilio
    assert normalize_phone(raw) == _E164


def test_send_sms_skips_unusable_number_without_raising(monkeypatch):
    """An unusable number must not raise - raising is what aborted the loop."""
    import app.services.sms as sms

    def _explode(*a, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("Twilio should not be called for an unusable number")

    monkeypatch.setattr(sms.httpx, "AsyncClient", _explode)
    assert asyncio.run(sms.send_sms(f"{_DIGITS}x22", "body")) is None


# ----- the per-manager loop guard -----
#
# The real Project Management shape: three supervisors on one employee. The
# first two both have numbers on file, so the SMS branch is genuinely entered
# for a manager positioned AFTER the failure.

_MANAGERS = [
    {"id": "1", "fields": {"Title": "First Supervisor", "EmailAddress": "first@example.invalid", "CellNumber": "4165550111"}},
    {"id": "2", "fields": {"Title": "Second Supervisor", "EmailAddress": "second@example.invalid", "CellNumber": "4165550112"}},
    {"id": "3", "fields": {"Title": "Third Supervisor", "EmailAddress": "third@example.invalid", "CellNumber": ""}},
]

#: Any location the app maps to a province - read from the source map rather than
#: naming a real site, so this keeps working if the office list changes.
_LOCATION = next(iter(LOCATION_PROVINCE_MAP))

_SUBMITTER = {
    "id": "100",
    "fields": {
        "Title": "Test Submitter",
        "EmailAddress": "submitter@example.invalid",
        "Location": _LOCATION,
        "CurrentVacationBalance": 10,
        "CarryOver": 0,
        "Payout": 0,
    },
}


def _manager_email(index: int) -> str:
    """Email of the supervisor at this position in the notification order."""
    return _MANAGERS[index]["fields"]["EmailAddress"]


def _manager_cell(index: int) -> str:
    """Stored phone of the supervisor at this position in the notification order."""
    return _MANAGERS[index]["fields"]["CellNumber"]


def _all_manager_emails() -> list[str]:
    """Every supervisor email, in the order the loop sends them."""
    return [m["fields"]["EmailAddress"] for m in _MANAGERS]


def _submitter_email() -> str:
    """Email of the employee who submitted the request."""
    return _SUBMITTER["fields"]["EmailAddress"]


def _drive_leave_approval(monkeypatch, *, email_raises_for=None, sms_raises_for=None):
    """Run the real leave send_approval_email with the provider calls stubbed.

    Everything outside the two send calls is replaced so the test observes only
    the loop's failure handling. Both channels are recorded, so a regression
    that skips one of them is visible.

    Args:
        monkeypatch: pytest fixture used to swap the module-level dependencies.
        email_raises_for: Email address whose send should raise, or None.
        sms_raises_for: Phone number whose send should raise, or None. This is
            the production trigger - Twilio rejecting the recipient.

    Returns:
        Tuple of (emailed addresses, texted numbers), in send order.
    """
    from app import templates_render
    from app.services import leave_requests

    fields = {
        "Status": "Pending",
        "ApproveProcessedFlag": "Not Processed",
        "ManagerLookupId": 42,
        "LeaveType": "Vacation",
        "Days": 1,
        "StartDate": "2026-09-01T04:00:00Z",
        "EndDate": "2026-09-01T04:00:00Z",
        "SubmittedTestLookupId": 7,
    }

    emailed: list[str] = []
    texted: list[str] = []

    async def fake_get_list_item(list_id, item_id):
        return {"id": str(item_id), "fields": fields}

    async def fake_resolve(_person_field):
        return _SUBMITTER

    async def fake_managers(_employee):
        return _MANAGERS

    async def fake_bump(*a, **k):
        return 1  # version 1 keeps previous_snapshot None, so no extra patching

    async def fake_email(to, **kwargs):
        recipient = to[0]
        # A collection lets a test fail several recipients at once, which is how
        # the "nobody was notified" case is set up.
        failing = (
            email_raises_for if isinstance(email_raises_for, (list, tuple, set))
            else {email_raises_for}
        )
        if recipient in failing:
            raise RuntimeError("provider rejected recipient")
        emailed.append(recipient)

    async def fake_sms(to=None, body=None):
        if to == sms_raises_for:
            # Mirrors send_sms letting a Twilio HTTPStatusError escape, which is
            # what a STOP reply or a suspended account produces in production.
            raise RuntimeError("twilio rejected recipient")
        texted.append(to)

    monkeypatch.setattr(leave_requests.sp_client, "get_list_item", fake_get_list_item)
    monkeypatch.setattr(leave_requests, "resolve_person_field", fake_resolve)
    monkeypatch.setattr(leave_requests, "get_all_managers_for_employee", fake_managers)
    monkeypatch.setattr(leave_requests, "simulate_leave_impact", lambda *a, **k: None)
    monkeypatch.setattr(leave_requests, "bump_and_snapshot", fake_bump)
    monkeypatch.setattr(leave_requests, "generate_approval_url", lambda *a, **k: "https://example.invalid/x")
    monkeypatch.setattr(leave_requests, "send_email_with_dashboard", fake_email)
    monkeypatch.setattr(leave_requests, "send_sms", fake_sms)
    monkeypatch.setattr(templates_render, "render_leave_approval_email", lambda *a, **k: "<html>")
    monkeypatch.setattr(templates_render, "render_leave_confirmation", lambda *a, **k: "<html>")

    asyncio.run(leave_requests.send_approval_email("3402"))
    return emailed, texted


def test_failing_text_does_not_silence_later_managers(monkeypatch):
    """The production regression: a Twilio failure on the first supervisor.

    Before the fix that raise ended the loop, so the supervisors after them
    received neither their email nor their text, and the submitter never got a
    confirmation. The first supervisor still gets their email, because it is
    sent before their text is attempted.
    """
    emailed, texted = _drive_leave_approval(monkeypatch, sms_raises_for=_manager_cell(0))

    assert emailed == _all_manager_emails() + [_submitter_email()]
    assert texted == [_manager_cell(1)]  # the next supervisor's text still went out


def test_failing_email_does_not_silence_later_managers(monkeypatch):
    """The same guard from the other direction: an email provider rejection.

    The first supervisor's email is rejected before their text is attempted, so
    they get neither - but the supervisors after them are unaffected on both
    channels.
    """
    emailed, texted = _drive_leave_approval(monkeypatch, email_raises_for=_manager_email(0))

    assert emailed == _all_manager_emails()[1:] + [_submitter_email()]
    assert texted == [_manager_cell(1)]


def test_no_failures_notifies_everyone(monkeypatch):
    """Baseline: with no provider failures every channel fires.

    Guards against the try/except quietly swallowing a real bug and the other
    two tests still passing for the wrong reason.
    """
    emailed, texted = _drive_leave_approval(monkeypatch)

    assert emailed == _all_manager_emails() + [_submitter_email()]
    assert texted == [_manager_cell(0), _manager_cell(1)]


def test_total_failure_raises_and_skips_the_submitter_confirmation(monkeypatch):
    """Every manager failing must stay loud, or the request is stranded.

    `change_processor` records an item as processed only when dispatch returns
    cleanly, so a swallowed total failure would mark the request done and forfeit
    the retry on the next delta query — nobody notified, and no second attempt.
    Raising preserves the retry. The submitter must also NOT be told "Received",
    because no manager has heard about it.
    """
    with pytest.raises(NotificationsFailed) as excinfo:
        _drive_leave_approval(monkeypatch, email_raises_for=_all_manager_emails())

    assert excinfo.value.attempted == len(_MANAGERS)
    assert "#3402" in str(excinfo.value)


def test_partial_failure_still_confirms_the_submitter(monkeypatch):
    """One manager reached is enough — the request is genuinely in flight.

    Pins the boundary of the test above: the raise must trigger on zero
    notifications, not on any failure.
    """
    emailed, _ = _drive_leave_approval(monkeypatch, email_raises_for=_manager_email(0))

    assert _submitter_email() in emailed


# ----- the same guard on the other two request types -----
#
# All three services carry an identical per-manager loop, so all three carry the
# identical failure mode. Leave is covered above; these cover the other two.


def _drive_overtime_approval(monkeypatch, *, sms_raises_for=None):
    """Run the real overtime send_approval_email with provider calls stubbed.

    Location is left as a genuine mapped value so map_location_to_province and
    the holiday/half-Friday branches run unpatched.

    Args:
        monkeypatch: pytest fixture used to swap module-level dependencies.
        sms_raises_for: Phone number whose send should raise, or None.

    Returns:
        Tuple of (emailed addresses, texted numbers), in send order.
    """
    from app import templates_render
    from app.services import overtime_requests

    fields = {"Status": "Pending", "StartDate": "2026-09-01T04:00:00Z", "Hours": 8}

    emailed: list[str] = []
    texted: list[str] = []

    async def fake_get_list_item(list_id, item_id):
        return {"id": str(item_id), "fields": fields}

    async def fake_holidays(_province):
        return []  # no holidays: skips the auto-reject and half-Friday branches

    async def fake_bump(*a, **k):
        return 1

    async def fake_email(to, **kwargs):
        emailed.append(to[0])

    async def fake_sms(to=None, body=None):
        if to == sms_raises_for:
            raise RuntimeError("twilio rejected recipient")
        texted.append(to)

    monkeypatch.setattr(overtime_requests.sp_client, "get_list_item", fake_get_list_item)
    monkeypatch.setattr(overtime_requests, "get_holidays_for_province", fake_holidays)
    monkeypatch.setattr(overtime_requests, "simulate_overtime_impact", lambda *a, **k: None)
    monkeypatch.setattr(overtime_requests, "bump_and_snapshot", fake_bump)
    monkeypatch.setattr(overtime_requests, "generate_approval_url", lambda *a, **k: "https://example.invalid/x")
    monkeypatch.setattr(overtime_requests, "send_email_with_dashboard", fake_email)
    monkeypatch.setattr(overtime_requests, "send_sms", fake_sms)
    monkeypatch.setattr(templates_render, "render_overtime_approval_email", lambda *a, **k: "<html>")
    monkeypatch.setattr(templates_render, "render_overtime_confirmation", lambda *a, **k: "<html>")

    asyncio.run(overtime_requests.send_approval_email("77", _SUBMITTER, _MANAGERS))
    return emailed, texted


def test_overtime_failing_text_does_not_silence_later_managers(monkeypatch):
    """Overtime carries the same loop and therefore the same regression."""
    emailed, texted = _drive_overtime_approval(monkeypatch, sms_raises_for=_manager_cell(0))

    assert emailed == _all_manager_emails() + [_submitter_email()]
    assert texted == [_manager_cell(1)]


def _drive_carryover_approval(monkeypatch, *, sms_raises_for=None):
    """Run the real carryover/payout send_approval_email with providers stubbed.

    Args:
        monkeypatch: pytest fixture used to swap module-level dependencies.
        sms_raises_for: Phone number whose send should raise, or None.

    Returns:
        Tuple of (emailed addresses, texted numbers), in send order. Each
        manager email also copies HR, so only the first recipient is recorded.
    """
    from app import templates_render
    from app.services import carryover_payout

    fields = {
        "Status": "Pending",
        "SystemState": "Not Processed",
        "EmployeeID": _SUBMITTER["id"],
        "Days": 1,
        "TypeofRequest": "Carry Over",
    }

    emailed: list[str] = []
    texted: list[str] = []

    async def fake_get_list_item(list_id, item_id):
        return {"id": str(item_id), "fields": fields}

    async def fake_employee_by_id(_employee_id):
        return _SUBMITTER

    async def fake_managers(_employee):
        return _MANAGERS

    async def fake_bump(*a, **k):
        return 1

    async def fake_email(to, **kwargs):
        emailed.append(to[0])  # to[1] is the standing HR copy

    async def fake_sms(to=None, body=None):
        if to == sms_raises_for:
            raise RuntimeError("twilio rejected recipient")
        texted.append(to)

    monkeypatch.setattr(carryover_payout.sp_client, "get_list_item", fake_get_list_item)
    monkeypatch.setattr(carryover_payout, "get_employee_by_id", fake_employee_by_id)
    monkeypatch.setattr(carryover_payout, "get_all_managers_for_employee", fake_managers)
    monkeypatch.setattr(carryover_payout, "bump_and_snapshot", fake_bump)
    monkeypatch.setattr(carryover_payout, "generate_approval_url", lambda *a, **k: "https://example.invalid/x")
    monkeypatch.setattr(carryover_payout, "send_email_with_dashboard", fake_email)
    monkeypatch.setattr(carryover_payout, "send_sms", fake_sms)
    monkeypatch.setattr(
        templates_render, "render_carryover_payout_approval_email", lambda *a, **k: "<html>"
    )

    asyncio.run(carryover_payout.send_approval_email("88"))
    return emailed, texted


def test_carryover_failing_text_does_not_silence_later_managers(monkeypatch):
    """Carry Over / Payout carries the same loop and the same regression."""
    emailed, texted = _drive_carryover_approval(monkeypatch, sms_raises_for=_manager_cell(0))

    # This pipeline has no submitter confirmation, so only the supervisors appear.
    assert emailed == _all_manager_emails()
    assert texted == [_manager_cell(1)]


# ----- silent returns must not be silent -----
#
# Every guard before the loop exits without notifying anyone. Diagnosing the July
# outage cost a full code audit precisely because those exits left no trace, so
# each one is pinned here.


def _run_leave_approval_with_fields(monkeypatch, fields):
    """Call the real leave send_approval_email against a bare SharePoint item.

    Only the item read is stubbed - the intent is to trip a guard before any
    provider call is reached, so nothing else needs replacing.

    Args:
        monkeypatch: pytest fixture used to swap the SharePoint read.
        fields: The SharePoint item's `fields` payload to return.
    """
    from app.services import leave_requests

    async def fake_get_list_item(list_id, item_id):
        return {"id": str(item_id), "fields": fields}

    monkeypatch.setattr(leave_requests.sp_client, "get_list_item", fake_get_list_item)
    asyncio.run(leave_requests.send_approval_email("3402"))


def test_missing_manager_lookup_id_is_logged(monkeypatch, caplog):
    """The likeliest cause of "nobody was notified" must name itself.

    Without ManagerLookupId the request notifies no one and is also hidden from
    the dashboards, so this warning is the only evidence it ever existed.
    """
    with caplog.at_level(logging.WARNING):
        _run_leave_approval_with_fields(
            monkeypatch, {"Status": "Pending", "ApproveProcessedFlag": "Not Processed"}
        )

    assert "ManagerLookupId" in caplog.text
    assert "3402" in caplog.text


def test_non_pending_status_is_logged(monkeypatch, caplog):
    """A normal skip still says so, at info rather than warning."""
    with caplog.at_level(logging.INFO):
        _run_leave_approval_with_fields(
            monkeypatch,
            {"Status": "Approved", "ApproveProcessedFlag": "Not Processed", "ManagerLookupId": 42},
        )

    assert "not Pending" in caplog.text
    assert "3402" in caplog.text
