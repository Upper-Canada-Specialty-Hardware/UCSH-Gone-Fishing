"""Requests from people not on staff yet: hold, remind, release (#144, #146).

Someone who verified an email that is on no staff record picks a supervisor
and fills in a form. The request is held and the supervisor emailed a
prefilled Add Employee link; reminders go to the supervisor after 2 business
days and to the admins after 5; adding the person submits the held request.
"""

import asyncio
import itertools
import time
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.models.mixins import utcnow
from app.routes import intake
from app.services import email_codes, held_requests, leave_requests

_counter = itertools.count()

SUPERVISOR = {"id": "3", "fields": {"Title": "Sam Boss", "EmailAddress": "sam@ucsh.com", "Location": "Barrie"}}
WORKER = {"id": "4", "fields": {"Title": "Kim Worker", "EmailAddress": "kim@ucsh.com",
                                "AllManagers": [{"LookupId": 20, "LookupValue": "Sam Boss"}]}}
ADMIN = {"id": "5", "fields": {"Title": "Jay Puzon", "EmailAddress": "jay@ucsh.com"}}
VACATION = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"}


def _email() -> str:
    """An address no earlier run has used (the test database keeps rows)."""
    return f"newhire{next(_counter)}-{time.time_ns()}@gmail.com"


@pytest.fixture
def world(monkeypatch):
    """Stub the Staff Directory, email and request creation.

    Returns:
        An object with .sent (emails), .staff_by_email (who is on staff),
        .created (requests submitted) and .linked (site user list).
    """
    class World:
        sent: list = []
        staff_by_email: dict = {}
        created: list = []
        linked = True
        fail_submit = False
    w = World()
    w.sent, w.staff_by_email, w.created = [], {}, []
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)

    async def staff_list(list_id, **kwargs):
        return [SUPERVISOR, WORKER, ADMIN]

    async def by_name(name):
        return {r["fields"]["Title"]: r for r in (SUPERVISOR, WORKER, ADMIN)}.get(name)

    async def by_email(email):
        return w.staff_by_email.get(email.strip().lower())

    async def fake_send(to, subject, html_body, **kwargs):
        w.sent.append({"to": to, "subject": subject, "html": html_body})

    async def fake_submit(request_type, form, employee, source):
        if w.fail_submit:
            raise RuntimeError("Graph 500")
        w.created.append((request_type, form.model_dump(mode="json"), employee["id"], source))
        return {"id": f"9{len(w.created)}"}

    async def lookup(email):
        return 12 if w.linked else None

    monkeypatch.setattr(held_requests.sp_client, "get_list_items", staff_list)
    monkeypatch.setattr(held_requests, "get_employee_by_name", by_name)
    monkeypatch.setattr(held_requests, "get_employee_by_email", by_email)
    monkeypatch.setattr(held_requests, "send_email", fake_send)
    monkeypatch.setattr(held_requests, "submit_request", fake_submit)
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", lookup)
    monkeypatch.setattr(intake, "get_employee_by_email", by_email)
    return w


def _hold(email, **overrides):
    """Hold a vacation request for ``email`` with Sam as supervisor."""
    args = dict(name="Lee New", location="Barrie", supervisor_id="3", request_type="leave", form=VACATION)
    args.update(overrides)
    return asyncio.run(held_requests.hold_request(email, **args))


# ----- pure helpers -----

def test_business_days_skip_the_weekend():
    friday = date(2026, 10, 9)
    assert held_requests.business_days_between(friday, friday) == 0
    assert held_requests.business_days_between(friday, date(2026, 10, 12)) == 1   # Monday
    assert held_requests.business_days_between(friday, date(2026, 10, 16)) == 5   # next Friday


def test_the_add_employee_link_is_a_signed_manager_link_with_the_prefill():
    url = held_requests.add_employee_url("3", "Lee New", "lee@gmail.com", "British Columbia")
    assert "role=manager" in url and "uid=3" in url
    assert "add_email=lee%40gmail.com" in url and "add_name=Lee+New" in url
    assert "add_location=British+Columbia" in url


def test_supervisors_are_the_people_named_in_all_managers(world):
    supervisors = asyncio.run(held_requests.list_supervisors())
    assert supervisors == [{"id": "3", "name": "Sam Boss", "location": "Barrie"}]


# ----- holding -----

def test_a_held_request_is_stored_and_the_supervisor_emailed(world):
    email = _email()
    row = _hold(email)

    assert row.status == "held" and row.supervisor_email == "sam@ucsh.com"
    assert row.form_data["end_date"] == "2026-11-03"
    [mail] = world.sent
    assert mail["to"] == ["sam@ucsh.com"] and "Lee New" in mail["subject"]
    assert "add_email=" in mail["html"] and "Vacation, 2026-11-02 to 2026-11-03" in mail["html"]


@pytest.mark.parametrize("overrides, message", [
    ({"name": "Lee"}, "first and last name"),
    ({"location": "Mars"}, "location"),
    ({"supervisor_id": "4"}, "supervisor"),                    # Kim supervises nobody
    ({"form": {**VACATION, "leave_type": "Nap"}}, "leave types"),
])
def test_a_bad_hold_is_refused_with_a_reason(world, overrides, message):
    with pytest.raises(ValueError, match=message):
        _hold(_email(), **overrides)
    assert world.sent == []


def test_one_address_can_only_have_a_few_waiting(world):
    email = _email()
    for _ in range(held_requests.MAX_OPEN_PER_EMAIL):
        _hold(email)
    with pytest.raises(held_requests.HoldError, match="several"):
        _hold(email)


# ----- the intake endpoints -----

@pytest.fixture
def client(world):
    app = FastAPI()
    app.include_router(intake.router, prefix="/api/intake")
    return TestClient(app)


def test_the_supervisor_list_needs_a_verified_email(client):
    assert client.get("/api/intake/supervisors", params={"email": "x@y.com", "exp": "1", "token": "no"}).status_code == 401
    good = email_codes.sign_verified_email("x@y.com")
    body = client.get("/api/intake/supervisors", params=good).json()
    assert body["supervisors"][0]["name"] == "Sam Boss" and "Barrie" in body["locations"]


def test_holding_through_the_endpoint(client, world):
    email = _email()
    body = {"verified": email_codes.sign_verified_email(email), "name": "Lee New", "location": "Barrie",
            "supervisor_id": "3", "request_type": "leave", "form": VACATION}
    response = client.post("/api/intake/held", json=body)
    assert response.status_code == 200 and response.json()["supervisor_name"] == "Sam Boss"


def test_someone_added_since_they_verified_is_sent_back_to_sign_in(client, world):
    email = _email()
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    body = {"verified": email_codes.sign_verified_email(email), "name": "Lee New", "location": "Barrie",
            "supervisor_id": "3", "request_type": "leave", "form": VACATION}
    assert client.post("/api/intake/held", json=body).status_code == 409


# ----- reminders -----

def test_the_supervisor_is_reminded_once_then_the_admins_once(world):
    email = _email()
    _hold(email)
    world.sent.clear()
    today = held_requests.toronto_date(utcnow())

    # Only rows for this test's address matter; others in the database may also be due.
    def mine():
        return [m for m in world.sent if email in m["html"]]

    def after(business_days):
        """The first date that many business days after today."""
        day = today
        while held_requests.business_days_between(today, day) < business_days:
            day += timedelta(days=1)
        return day

    asyncio.run(held_requests.remind_held_requests(after(1)))
    assert mine() == []                                        # not yet due

    asyncio.run(held_requests.remind_held_requests(after(2)))
    assert [m["to"] for m in mine()] == [["sam@ucsh.com"]]

    asyncio.run(held_requests.remind_held_requests(after(5)))
    assert [m["to"] for m in mine()] == [["sam@ucsh.com"], ["jay@ucsh.com"]]

    asyncio.run(held_requests.remind_held_requests(after(20)))
    assert len(mine()) == 2                                    # never repeated


# ----- releasing -----

def test_adding_the_person_submits_their_held_requests_and_tells_them(world):
    email = _email()
    _hold(email)
    _hold(email, request_type="overtime", form={"description": "Setup", "date": "2026-11-05", "hours": 2})
    world.sent.clear()
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}

    results = asyncio.run(held_requests.release_held_requests(email))

    assert [r.status for r in results] == ["released", "released"]
    assert [c[0] for c in world.created] == ["leave", "overtime"]
    assert all(c[2] == "8" and c[3] == "Request page" for c in world.created)
    [mail] = world.sent
    assert mail["to"] == [email] and "Setup" in mail["html"]
    assert asyncio.run(held_requests.release_held_requests(email)) == []   # nothing left to release


def test_a_failed_release_is_kept_for_a_retry(world):
    email = _email()
    _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    world.fail_submit = True

    [result] = asyncio.run(held_requests.release_held_requests(email))
    assert result.status == "failed" and "Graph 500" in result.detail

    world.fail_submit = False
    [retry] = asyncio.run(held_requests.release_held_requests(email, only_id=result.held_id))
    assert retry.status == "released"


def test_without_the_email_column_an_unlinked_person_waits_for_the_site(world, monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", False)
    world.linked = False
    email = _email()
    _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}

    [result] = asyncio.run(held_requests.release_held_requests(email))
    assert result.status == "waiting_site" and world.created == []

    # Already added, so the supervisor is not told to add them again.
    world.sent.clear()
    asyncio.run(held_requests.remind_held_requests(today=date(2099, 1, 5)))
    assert not any(email in m["html"] for m in world.sent)

    # Once they reach the site, the hourly retry sends the request on.
    world.linked = True
    asyncio.run(held_requests.retry_waiting_site())
    assert len(world.created) == 1
    listed = asyncio.run(held_requests.list_held_requests(include_closed=True))
    assert next(r for r in listed if r["email"] == email)["status"] == "released"


def test_a_row_claimed_by_another_release_is_not_submitted_twice(world):
    email = _email()
    row = _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}

    async def both():
        return await asyncio.gather(
            held_requests.release_held_requests(email),
            held_requests.release_held_requests(email, only_id=row.id),
        )

    asyncio.run(both())
    assert len(world.created) == 1


def test_someone_still_not_on_staff_releases_nothing(world):
    email = _email()
    _hold(email)
    assert asyncio.run(held_requests.release_held_requests(email)) == []


def test_a_cancelled_request_is_never_released(world):
    email = _email()
    row = _hold(email)
    assert asyncio.run(held_requests.cancel_held_request(row.id))
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    assert asyncio.run(held_requests.release_held_requests(email)) == []


# ----- Add Employee hands off to the release -----

def test_add_employee_reports_what_was_released(monkeypatch):
    from app.routes import dashboard

    async def fake_release(email):
        assert email == "lee@gmail.com"
        return [held_requests.ReleaseResult(1, "released", "91")]

    monkeypatch.setattr(held_requests, "release_held_requests", fake_release)
    record = asyncio.run(dashboard._after_employee_created(
        {"id": "8", "fields": {"EmailAddress": "lee@gmail.com"}, "notices": []}))
    assert record["released"] == [{"held_id": 1, "status": "released", "detail": "91"}]


def test_a_release_error_never_undoes_the_new_employee(monkeypatch):
    from app.routes import dashboard

    async def broken(email):
        raise RuntimeError("database down")

    monkeypatch.setattr(held_requests, "release_held_requests", broken)
    record = asyncio.run(dashboard._after_employee_created(
        {"id": "8", "fields": {"EmailAddress": "lee@gmail.com"}, "notices": []}))
    assert record["id"] == "8" and record["released"] == []
    assert "Held Requests" in record["notices"][0]
