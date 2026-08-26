"""Tests for the leave-request description that managers see in emails.

Three parts: the Title <-> description helpers, the rendered email templates
(real Jinja against app/templates/emails -- conftest chdirs to backend/, so the
FileSystemLoader path resolves), and the intake that stores the Title.
"""
import asyncio

import pytest

from app.services import leave_requests
from app.services.request_descriptions import (
    compose_leave_title,
    extract_request_description,
)
from app.templates_render import (
    render_bereavement_alert,
    render_leave_approval_email,
    render_leave_approved,
    render_leave_confirmation,
    render_leave_hourly_approved,
    render_leave_rejected,
    render_partial_day_holiday_rejected,
)

APPROVAL_EMAIL_ARGS = dict(
    emp_fields={"CurrentVacationBalance": 10},
    approve_url="https://example.com/approve",
    reject_url="https://example.com/reject",
    submitter_name="Jane Doe",
)


def _leave_fields(title, leave_type="Half Day or Partial Day Off"):
    return {
        "Title": title,
        "LeaveType": leave_type,
        "StartDate": "2026-08-03",
        "EndDate": "2026-08-03",
        "Days": 0.5,
    }


# --- extract_request_description -------------------------------------------

def test_extract_splits_compound_leave_title():
    assert extract_request_description(
        "Jane Doe /// Doctor's appointment", "leave"
    ) == "Doctor's appointment"


def test_extract_returns_empty_for_name_only_leave_title():
    # No separator == no description was captured, not "the name is the description".
    assert extract_request_description("Jane Doe", "leave") == ""


@pytest.mark.parametrize("title", [None, "", "   "])
def test_extract_handles_missing_title(title):
    assert extract_request_description(title, "leave") == ""


def test_extract_keeps_separators_inside_the_description():
    # Split on the FIRST separator only, so the description survives intact.
    assert extract_request_description(
        "Jane Doe /// travel /// then medical", "leave"
    ) == "travel /// then medical"


def test_extract_returns_empty_when_description_is_blank():
    assert extract_request_description("Jane Doe /// ", "leave") == ""


def test_extract_retains_auto_reject_tag():
    # append_auto_reject_tag() appends to Title; the tag belongs to the description half.
    assert extract_request_description(
        "Jane Doe /// Doctor's appointment [Auto-Rejected: holiday]", "leave"
    ) == "Doctor's appointment [Auto-Rejected: holiday]"


def test_extract_leaves_non_leave_titles_whole():
    # Overtime Titles are the description already -- splitting would corrupt them.
    assert extract_request_description(
        "Covering the Friday shift", "overtime"
    ) == "Covering the Friday shift"


# --- compose_leave_title ----------------------------------------------------

def test_compose_joins_name_and_notes():
    assert compose_leave_title(
        "Jane Doe", "Doctor's appointment"
    ) == "Jane Doe /// Doctor's appointment"


@pytest.mark.parametrize("notes", [None, "", "   "])
def test_compose_omits_separator_without_notes(notes):
    assert compose_leave_title("Jane Doe", notes) == "Jane Doe"


def test_compose_keeps_separator_when_name_is_missing():
    # Otherwise the description would look like a bare name and extract to "".
    assert compose_leave_title(None, "Doctor's appointment") == " /// Doctor's appointment"


@pytest.mark.parametrize(
    "name",
    ["Jane Doe", None, "", "  Jane Doe  "],
)
def test_compose_round_trips_through_extract(name):
    title = compose_leave_title(name, "Doctor's appointment")
    assert extract_request_description(title, "leave") == "Doctor's appointment"


# --- rendered emails --------------------------------------------------------

def test_approval_email_shows_the_description_to_the_manager():
    html = render_leave_approval_email(
        fields=_leave_fields("Jane Doe /// Doctor's appointment"), **APPROVAL_EMAIL_ARGS
    )
    assert "Description:" in html
    assert "Doctor&#39;s appointment" in html
    # The name belongs on the "Requested by" row -- never in the description.
    assert "///" not in html


def test_approval_email_omits_the_row_when_there_is_no_description():
    html = render_leave_approval_email(fields=_leave_fields("Jane Doe"), **APPROVAL_EMAIL_ARGS)
    assert "Description:" not in html


def test_approval_email_escapes_the_employees_free_text():
    html = render_leave_approval_email(
        fields=_leave_fields("Jane Doe /// <script>alert(1)</script>"), **APPROVAL_EMAIL_ARGS
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "render",
    [
        lambda fields: render_leave_approved(fields, "Bob Boss"),
        lambda fields: render_leave_rejected(fields, "Bob Boss"),
        lambda fields: render_leave_hourly_approved(fields, "Bob Boss"),
        lambda fields: render_leave_confirmation(fields, {}, None),
        lambda fields: render_bereavement_alert(fields, "Jane Doe"),
        lambda fields: render_partial_day_holiday_rejected(fields, "Canada Day", "reason"),
    ],
)
def test_every_leave_email_shows_the_description(render):
    html = render(_leave_fields("Jane Doe /// Doctor's appointment"))
    assert "Doctor&#39;s appointment" in html
    assert "///" not in html


@pytest.mark.parametrize(
    "render",
    [
        lambda fields: render_leave_approved(fields, "Bob Boss"),
        lambda fields: render_leave_rejected(fields, "Bob Boss"),
        lambda fields: render_leave_hourly_approved(fields, "Bob Boss"),
        lambda fields: render_leave_confirmation(fields, {}, None),
        lambda fields: render_bereavement_alert(fields, "Jane Doe"),
        lambda fields: render_partial_day_holiday_rejected(fields, "Canada Day", "reason"),
    ],
)
def test_every_leave_email_omits_an_empty_description(render):
    html = render(_leave_fields("Jane Doe"))
    assert "Description:" not in html


# --- POST /forms/leave intake ----------------------------------------------

def _submit_leave(monkeypatch, form_data):
    """Run process_new_leave_request with SharePoint and the follow-up tasks stubbed.

    Returns the fields dict that would have been written to the SP list item.
    """
    captured = {}

    async def _fake_create(list_id, fields):
        captured.update(fields)
        return {"id": "999", "fields": fields}

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(leave_requests.sp_client, "create_list_item", _fake_create)
    # No lookup id -> overlap detection is skipped, so no Graph calls.
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", _noop)
    monkeypatch.setattr(leave_requests, "auto_calculate_days", _noop)
    monkeypatch.setattr(leave_requests, "auto_assign_manager", _noop)
    monkeypatch.setattr(leave_requests, "send_bereavement_alert", _noop)

    asyncio.run(leave_requests.process_new_leave_request(form_data, "jane@ucsh.com"))
    return captured


def test_partial_day_intake_keeps_the_employees_notes(monkeypatch):
    # The reported case: a half day whose description the manager could not see.
    fields = _submit_leave(monkeypatch, {
        "leave_type": "Half Day or Partial Day Off",
        "start_date": "2026-08-03",
        "employee_name": "Jane Doe",
        "notes": "Doctor's appointment",
        "partial_hours": 4,
    })

    assert fields["Title"] == "Jane Doe /// Doctor's appointment"
    assert extract_request_description(fields["Title"], "leave") == "Doctor's appointment"


def test_full_day_intake_keeps_the_employees_notes(monkeypatch):
    fields = _submit_leave(monkeypatch, {
        "leave_type": "Vacation",
        "start_date": "2026-08-03",
        "end_date": "2026-08-05",
        "first_name": "Jane",
        "last_name": "Doe",
        "notes": "Family trip",
    })

    assert fields["Title"] == "Jane Doe /// Family trip"


def test_intake_without_notes_stores_a_bare_name(monkeypatch):
    fields = _submit_leave(monkeypatch, {
        "leave_type": "Vacation",
        "start_date": "2026-08-03",
        "end_date": "2026-08-05",
        "first_name": "Jane",
        "last_name": "Doe",
        "notes": None,
    })

    assert fields["Title"] == "Jane Doe"
    assert extract_request_description(fields["Title"], "leave") == ""


def test_intake_tolerates_omitted_name_fields(monkeypatch):
    # first_name/last_name/notes are all optional on LeaveFormData, so model_dump()
    # hands them over as None -- they must not land in the Title as "None None".
    fields = _submit_leave(monkeypatch, {
        "leave_type": "Vacation",
        "start_date": "2026-08-03",
        "end_date": "2026-08-05",
        "first_name": None,
        "last_name": None,
        "notes": "Family trip",
    })

    assert "None" not in fields["Title"]
    assert extract_request_description(fields["Title"], "leave") == "Family trip"
