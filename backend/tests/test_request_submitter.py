"""Finding a request's submitter by email first, then the person column (#140).

Requests from the public request page may carry only SubmitterEmail, because
their submitter never visited the SharePoint site and so cannot be named in a
person column. Older requests carry only the person column. Both must lead to
the same Staff Directory record, and the new columns are only written once the
REQUEST_EMAIL_COLUMNS_ENABLED setting says they exist.
"""

import asyncio

import pytest

from app.config import settings
from app.routes import dashboard
from app.services import leave_requests, request_submitter

PAT = {"id": "7", "fields": {"Title": "Pat Staff", "EmailAddress": "Pat@UCSH.com"}}


@pytest.fixture
def directory(monkeypatch):
    """Stub the two lookups: by email (Pat only) and by person column (id 12 = Pat).

    Returns:
        A dict counting how often each lookup ran.
    """
    calls = {"email": 0, "person": 0}

    async def by_email(email):
        calls["email"] += 1
        return PAT if email.strip().lower() == "pat@ucsh.com" else None

    async def by_person(value):
        calls["person"] += 1
        return PAT if str(value) == "12" else None

    async def name_by_person(value):
        return "Pat (from person column)" if str(value) == "12" else ""

    monkeypatch.setattr(request_submitter, "get_employee_by_email", by_email)
    monkeypatch.setattr(request_submitter, "resolve_person_field", by_person)
    monkeypatch.setattr(request_submitter, "resolve_person_field_name", name_by_person)
    return calls


# ----- writing the columns -----

def test_nothing_is_written_while_the_setting_is_off(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", False)
    assert request_submitter.submitter_columns("pat@ucsh.com", "Request page") == {}


def test_both_columns_are_written_once_enabled(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)
    assert request_submitter.submitter_columns(" Pat@UCSH.com ", "Request page") == {
        "SubmitterEmail": "pat@ucsh.com",
        "RequestSource": "Request page",
    }


def test_a_new_leave_item_carries_the_columns_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)
    created = {}

    async def fake_create(list_id, fields):
        created.update(fields)
        return {"id": "99", "fields": fields}

    async def no_lookup(email):
        return None                                            # never visited the site

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(leave_requests.sp_client, "create_list_item", fake_create)
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", no_lookup)
    for task in ("auto_calculate_days", "auto_assign_manager", "send_bereavement_alert"):
        monkeypatch.setattr(leave_requests, task, nothing)

    form = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03",
            "first_name": "Pat", "last_name": "Staff"}
    asyncio.run(leave_requests.process_new_leave_request(form, "pat@ucsh.com", "Request page"))

    assert created["SubmitterEmail"] == "pat@ucsh.com"
    assert created["RequestSource"] == "Request page"


# ----- reading them -----

def test_the_email_column_finds_the_submitter_without_the_person_column(directory):
    found = asyncio.run(request_submitter.resolve_request_submitter(
        {"SubmitterEmail": "pat@ucsh.com"}, "SubmittedTest"))
    assert found is PAT
    assert directory["person"] == 0                            # person column never needed


def test_an_older_request_still_resolves_through_the_person_column(directory):
    found = asyncio.run(request_submitter.resolve_request_submitter(
        {"SubmittedByLookupId": 12}, "SubmittedBy"))
    assert found is PAT
    assert directory["email"] == 0                             # no email to try


def test_an_unknown_email_falls_back_to_the_person_column(directory):
    found = asyncio.run(request_submitter.resolve_request_submitter(
        {"SubmitterEmail": "gone@ucsh.com", "SubmittedTestLookupId": 12}, "SubmittedTest"))
    assert found is PAT


def test_the_name_comes_from_the_directory_when_the_email_is_known(directory):
    name = asyncio.run(request_submitter.resolve_request_submitter_name(
        {"SubmitterEmail": "pat@ucsh.com", "SubmittedTestLookupId": 12}, "SubmittedTest"))
    assert name == "Pat Staff"


def test_the_dashboard_names_a_request_by_its_email():
    names = dashboard._PersonNames()
    names.by_email["pat@ucsh.com"] = "Pat Staff"

    assert dashboard._resolve_sp_user_name({"SubmitterEmail": "PAT@ucsh.com"}, "SubmittedTest", names) == "Pat Staff"
    # Manager columns never use the submitter's email.
    assert dashboard._resolve_sp_user_name({"SubmitterEmail": "pat@ucsh.com"}, "Manager", names) == ""


def test_the_dashboard_still_names_older_requests_by_person_column():
    names = dashboard._PersonNames()
    names[12] = "Pat Staff"
    assert dashboard._resolve_sp_user_name({"SubmittedByLookupId": 12}, "SubmittedBy", names) == "Pat Staff"
    # A plain dict (any older caller) still works.
    assert dashboard._resolve_sp_user_name({"SubmittedByLookupId": 12}, "SubmittedBy", {12: "Pat"}) == "Pat"


# ----- adding the columns -----

def test_missing_columns_are_reported_then_added(monkeypatch):
    monkeypatch.setattr(request_submitter.sp_client, "site_id", "site")
    posted = []

    async def fake_get(path, params=None):
        # The leave list already has SubmitterEmail; the others have neither.
        names = [{"name": "Title"}]
        if settings.SP_LIST_LEAVE_REQUESTS in path:
            names.append({"name": "SubmitterEmail"})
        return {"value": names}

    async def fake_post(path, json=None):
        posted.append((path, json["name"]))
        return {}

    monkeypatch.setattr(request_submitter.graph_client, "get", fake_get)
    monkeypatch.setattr(request_submitter.graph_client, "post", fake_post)

    report = asyncio.run(request_submitter.ensure_request_columns(create=False))
    assert not report["ready"] and posted == []
    assert report["lists"]["leave"]["missing"] == ["RequestSource"]

    report = asyncio.run(request_submitter.ensure_request_columns(create=True))
    assert report["ready"]
    assert len(posted) == 5                                    # 1 on leave, 2 on each of the others


def test_a_failing_list_is_reported_without_stopping_the_others(monkeypatch):
    monkeypatch.setattr(request_submitter.sp_client, "site_id", "site")

    async def fake_get(path, params=None):
        if settings.SP_LIST_OVERTIME_REQUESTS in path:
            raise RuntimeError("403 Forbidden")
        return {"value": [{"name": "SubmitterEmail"}, {"name": "RequestSource"}]}

    monkeypatch.setattr(request_submitter.graph_client, "get", fake_get)

    report = asyncio.run(request_submitter.ensure_request_columns(create=True))
    assert report["lists"]["overtime"]["error"] == "403 Forbidden"
    assert report["lists"]["leave"]["present"] == ["SubmitterEmail", "RequestSource"]
    assert not report["ready"]
