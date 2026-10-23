"""One held request per new hire until they are set up (#442, #443, #445).

Covers the one-request-until-set-up gate (on sign-in and on submit), "not on my
team" and the re-pick, the one-thread-per-request headers, the reminder skip for
someone already on staff, cancel-with-a-reason and its undo, and the
add-employee name clash.

Like test_held_requests, this stubs the Staff Directory, email and request
creation so nothing real is called; emails are captured with their threading
headers.
"""
import asyncio
import itertools
import time
import types
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.graph.sharepoint import sp_client
from app.routes import intake
from app.services import email_codes, held_requests, leave_requests

_counter = itertools.count()


def _email() -> str:
    """An address no earlier run has used (the test database keeps rows)."""
    return f"newhire{next(_counter)}-{time.time_ns()}@gmail.com"


# Two supervisors in one department, so a decline can be re-picked to the other.
STEVE = {"id": "3", "fields": {"Title": "Steve Faith", "EmailAddress": "stevef@ucsh.com",
                               "Department": "Project Management"}}
EDUARDO = {"id": "4", "fields": {"Title": "Eduardo Del Sarto", "EmailAddress": "eduardod@ucsh.com",
                                 "Department": "Project Management"}}
VACATION = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"}


@pytest.fixture
def world(monkeypatch):
    """Stub the Staff Directory, email (with headers) and request creation.

    Returns:
        An object with .sent (each {"to","subject","html","headers"}),
        .staff_by_email, .staff_by_name and .created (requests submitted).
    """
    class World:
        sent: list = []
        staff_by_email: dict = {}
        staff_by_name: dict = {}
        created: list = []
    w = World()
    w.sent, w.staff_by_email, w.staff_by_name, w.created = [], {}, {}, []
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)

    async def staff_list(list_id, **kwargs):
        return [STEVE, EDUARDO]

    async def by_email(email):
        return w.staff_by_email.get((email or "").strip().lower())

    async def by_name(name):
        return w.staff_by_name.get(name)

    async def fake_send(to, subject, html_body, **kwargs):
        w.sent.append({"to": to, "subject": subject, "html": html_body, "headers": kwargs.get("headers")})

    async def fake_submit(request_type, form, employee, source, **kwargs):
        w.created.append((request_type, employee["id"], source))
        return {"id": f"7{len(w.created)}"}

    async def lookup(email):
        return 12

    monkeypatch.setattr(sp_client, "get_list_items", staff_list)
    monkeypatch.setattr(held_requests, "get_employee_by_email", by_email)
    monkeypatch.setattr(held_requests, "get_employee_by_name", by_name)
    monkeypatch.setattr(held_requests, "send_email", fake_send)
    monkeypatch.setattr(held_requests, "submit_request", fake_submit)
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", lookup)
    return w


def _hold(email, **overrides):
    """Hold a new hire's request to Steve's team by default.

    Each call gets a fresh email unless one is given, so the shared test database
    (rows persist between tests) never matches the one-open gate on a leftover
    row from an earlier test.
    """
    args = dict(
        email=email, name="Lee New", department="Project Management",
        supervisor_id="3", request_type="leave", form=VACATION,
    )
    args.update(overrides)
    return asyncio.run(held_requests.hold_request(**args))


# ----- #443: threading headers -----

def test_thread_root_is_stable_and_messages_are_unique():
    # The conversation root is stable per (held id, recipient), ignoring case and
    # spacing, and differs for another request or recipient.
    root = held_requests.thread_root_id(5, "sam@ucsh.com")
    assert held_requests.thread_root_id(5, "  SAM@UCSH.com ") == root
    assert held_requests.thread_root_id(6, "sam@ucsh.com") != root
    assert held_requests.thread_root_id(5, "kim@ucsh.com") != root

    def value(headers, name):
        return next(h["value"] for h in headers if h["header"] == name)

    one = held_requests.thread_headers(5, "sam@ucsh.com")
    two = held_requests.thread_headers(5, "sam@ucsh.com")
    assert value(one, "In-Reply-To") == root and value(one, "References") == root
    assert value(one, "Message-ID") != value(two, "Message-ID")   # a fresh id each message


# ----- #442: the one-open gate -----

def test_open_held_request_for_matches_email_and_closes(world):
    email = _email()
    row = _hold(email)
    assert asyncio.run(held_requests.open_held_request_for(email=email)).id == row.id
    asyncio.run(held_requests.cancel_held_request(row.id, "duplicate"))   # closes it
    assert asyncio.run(held_requests.open_held_request_for(email=email)) is None


# ----- #443: not on my team, and the re-pick -----

def test_decline_then_repick_moves_to_a_new_supervisor_and_tells_everyone(world):
    email = _email()
    row = _hold(email)
    world.sent.clear()

    assert asyncio.run(held_requests.decline_held_request(row.id, "3")) == "declined"
    assert any(m["to"] == [email] for m in world.sent)            # the person is told
    status = asyncio.run(held_requests.held_request_status(row.id))
    assert status["status"] == "not_on_team" and status["department"] == "Project Management"

    world.sent.clear()
    updated = asyncio.run(held_requests.repick_supervisor(row.id, email, "4", None))
    assert updated.supervisor_name == "Eduardo Del Sarto" and updated.status == "held"
    recipients = [m["to"] for m in world.sent]
    assert ["eduardod@ucsh.com"] in recipients                   # the new supervisor is asked
    assert ["stevef@ucsh.com"] in recipients                     # the decliner is told it moved
    assert [email] in recipients                                 # the person is told


def test_repick_refuses_the_supervisor_who_declined(world):
    email = _email()
    row = _hold(email)
    asyncio.run(held_requests.decline_held_request(row.id, "3"))
    with pytest.raises(held_requests.HoldError, match="already said"):
        asyncio.run(held_requests.repick_supervisor(row.id, email, "3", None))


def test_a_stale_not_on_team_link_cannot_decline_again(world):
    email = _email()
    row = _hold(email)
    assert asyncio.run(held_requests.decline_held_request(row.id, "3")) == "declined"
    # Already not_on_team: the same link is now a no-op, not a second decline.
    assert asyncio.run(held_requests.decline_held_request(row.id, "3")) == "already"


def test_the_not_on_team_token_round_trips_and_is_guarded():
    token = held_requests.not_on_team_token(5, "3")
    assert held_requests.decode_not_on_team_token(token) == (5, "3")
    # A different supervisor id in the token does not verify.
    forged = held_requests.not_on_team_token(5, "4").rsplit(".", 1)[0] + "." + token.rsplit(".", 1)[1]
    assert held_requests.decode_not_on_team_token(forged) is None
    assert held_requests.decode_not_on_team_token("nonsense") is None


# ----- #445: reminders skip on-staff, cancel needs a reason, undo -----

def test_reminders_skip_a_new_hire_already_on_staff(world):
    # A new hire still not on staff is reminded; one already added is skipped,
    # because the release will submit their request (#445).
    waiting = _email()
    _hold(waiting)
    added = _email()
    _hold(added)
    world.staff_by_email[added] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": added}}
    world.sent.clear()
    asyncio.run(held_requests.remind_held_requests(date(2099, 1, 5)))
    assert ["stevef@ucsh.com"] in [m["to"] for m in world.sent]   # the waiting one is reminded
    assert not any(added in (m["html"] or "") for m in world.sent)   # the added one is skipped


def test_cancel_needs_a_reason_and_can_be_undone(world):
    email = _email()
    row = _hold(email)
    with pytest.raises(held_requests.HoldError, match="reason is required"):
        asyncio.run(held_requests.cancel_held_request(row.id, "  "))
    assert asyncio.run(held_requests.cancel_held_request(row.id, "wrong team"))
    listed = asyncio.run(held_requests.list_held_requests(include_closed=True))
    cancelled = next(r for r in listed if r["id"] == row.id)
    assert cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "wrong team"
    assert asyncio.run(held_requests.uncancel_held_request(row.id))
    back = next(r for r in asyncio.run(held_requests.list_held_requests()) if r["id"] == row.id)
    assert back["status"] == "held" and back["cancel_reason"] is None


def test_the_held_list_shows_on_staff_and_routes_to(world):
    email = _email()
    _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    listed = asyncio.run(held_requests.list_held_requests())
    mine = next(r for r in listed if r["email"] == email)
    assert mine["on_staff"] is True and mine["routes_to"] == "Steve Faith"
    assert "kind" not in mine and "suggested_changes" not in mine   # mover fields are gone


# ----- #442: the sign-in gate (via /verify) -----

@pytest.fixture
def intake_client(world, monkeypatch):
    """A client for the intake router with the code check and lookups stubbed."""
    async def check_code(email, code):
        return types.SimpleNamespace(ok=True, error=None)

    async def by_email_active(email):
        record = world.staff_by_email.get((email or "").strip().lower())
        return [record] if record else []

    async def not_archived(email):
        return False

    monkeypatch.setattr(email_codes, "check_code", check_code)
    monkeypatch.setattr(intake, "get_employees_by_email", by_email_active)
    monkeypatch.setattr(intake, "email_belongs_to_archived", not_archived)
    app = FastAPI()
    app.include_router(intake.router, prefix="/api/intake")
    return TestClient(app)


def test_sign_in_shows_held_status_for_a_waiting_new_hire(intake_client, world):
    email = _email()
    _hold(email)
    data = intake_client.post("/api/intake/verify", json={"email": email, "code": "000000"}).json()
    assert data["status"] == "unknown"                            # not on staff
    assert data["held"]["supervisor_name"] == "Steve Faith" and data["held"]["state"] == "waiting"


# ----- #443: a GET does not decline (Safe Links), the POST does -----

def test_not_on_team_get_changes_nothing_then_post_declines(intake_client, world):
    email = _email()
    row = _hold(email)
    token = held_requests.not_on_team_token(row.id, "3")          # issued to Steve

    # A GET (a mail scanner prefetch) only shows the confirm page; it declines nothing.
    got = intake_client.get("/api/intake/held/not-on-team", params={"token": token})
    assert got.status_code == 200
    assert asyncio.run(held_requests.open_held_request_for(email=email)).status == "held"

    # The POST from the button actually hands it back.
    posted = intake_client.post("/api/intake/held/not-on-team", params={"token": token})
    assert posted.status_code == 200
    assert asyncio.run(held_requests.open_held_request_for(email=email)).status == "not_on_team"


# ----- #442: the submit gate (409) -----

def test_submit_is_refused_while_a_request_waits_to_be_set_up(world, monkeypatch):
    from app.routes import dashboard as dash
    from app.routes import self_service
    from app.services import dashboard_tokens

    email = _email()
    _hold(email)                                                  # a held request is in flight
    record = {"id": "9", "fields": {"Title": "Lee New", "EmailAddress": email}}

    async def by_id(uid):
        return record

    async def no_archived_caller(role, uid):
        return None

    monkeypatch.setattr(self_service, "get_employee_by_id", by_id)
    monkeypatch.setattr(self_service, "is_archived", lambda fields: False)
    monkeypatch.setattr(dash, "refuse_if_archived_caller", no_archived_caller)

    app = FastAPI()
    app.include_router(self_service.router, prefix="/api/dashboard")
    client = TestClient(app)
    token = dashboard_tokens.generate_dashboard_token("employee", "9")
    response = client.post("/api/dashboard/me/requests/leave", params=token, json=VACATION)
    assert response.status_code == 409
    assert "waiting to be set up" in response.json()["detail"]


# ----- #444: the add-employee name clash -----

def test_add_employee_name_clash_returns_409_with_the_existing_id(monkeypatch):
    from app.routes import dashboard as dash
    from app.services import employee_creation as ec

    async def boom(form_data, manager_ids):
        raise ec.EmployeeNameClash("An employee named 'Anthony Talic' already exists.", "42")

    monkeypatch.setattr(ec, "create_employee", boom)
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    app = FastAPI()
    app.include_router(dash.router, prefix="/api/dashboard")
    client = TestClient(app)
    response = client.post("/api/dashboard/admin/employees", json={"manager_ids": [3]})
    assert response.status_code == 409
    assert response.json()["detail"]["existing_id"] == "42"


def test_create_employee_raises_a_name_clash_carrying_the_existing_id(monkeypatch):
    from app.services import employee_creation as ec

    async def clashes(name, email):
        return {"name": {"id": "42", "fields": {"Title": "Anthony Talic"}}, "email": None}

    monkeypatch.setattr(ec, "find_directory_clashes", clashes)
    form = {
        "title": "Anthony Talic", "email_address": "a@x.com", "location": "British Columbia",
        "department": "Production", "salary_hourly": "Salary",
        "vacation_entitlement": 10, "sick_entitlement": 5,
    }
    with pytest.raises(ec.EmployeeNameClash) as caught:
        asyncio.run(ec.create_employee(form, [3]))
    assert caught.value.existing_id == "42"


# ----- #462: stale links say what happened, and one conversation per request -----

def _root(headers):
    """The In-Reply-To a threaded email points at (its conversation root)."""
    return next(h["value"] for h in headers if h["header"] == "In-Reply-To")


def _numbered_submit(world, monkeypatch, manager_id=""):
    """Make each created request get a number no earlier run used, and a manager.

    The test database keeps rows between runs, so a fixed number could match an
    old released row. Returns the list the numbers are appended to.
    """
    numbers: list[int] = []

    async def submit(request_type, form, employee, source, **kwargs):
        numbers.append(time.time_ns() % 1_000_000_000)          # unique enough per run
        world.created.append((request_type, employee["id"], source))
        return {"id": str(numbers[-1]), "fields": {"ManagerID": manager_id} if manager_id else {}}

    monkeypatch.setattr(held_requests, "submit_request", submit)
    return numbers


def test_a_stale_link_says_what_happened(intake_client, world, monkeypatch):
    numbers = _numbered_submit(world, monkeypatch)
    email = _email()
    row = _hold(email)
    steve = held_requests.not_on_team_token(row.id, "3")
    intake_client.post("/api/intake/held/not-on-team", params={"token": steve})   # Steve declines

    # Steve opens his link again: he already said so, nothing more.
    again = intake_client.get("/api/intake/held/not-on-team", params={"token": steve}).text
    assert "Already done" in again and "not on your team" in again and "<form" not in again

    # It moves to Eduardo, who adds the person: Steve still learns nothing about where it went.
    asyncio.run(held_requests.repick_supervisor(row.id, email, "4", None))
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    asyncio.run(held_requests.release_held_requests(email))
    later = intake_client.get("/api/intake/held/not-on-team", params={"token": steve}).text
    assert "Already done" in later and "Eduardo" not in later

    # Eduardo's link, opened late, says it was handled and gives the request number.
    eduardo = held_requests.not_on_team_token(row.id, "4")
    for page in (intake_client.get("/api/intake/held/not-on-team", params={"token": eduardo}).text,
                 intake_client.post("/api/intake/held/not-on-team", params={"token": eduardo}).text):
        assert "Already handled" in page and "was set up on" in page and f"#{numbers[0]}" in page


def test_a_cancelled_request_and_a_forged_link(intake_client, world):
    email = _email()
    row = _hold(email)
    asyncio.run(held_requests.cancel_held_request(row.id, "duplicate"))
    page = intake_client.get("/api/intake/held/not-on-team",
                             params={"token": held_requests.not_on_team_token(row.id, "3")}).text
    assert "No longer needed" in page and "cancelled" in page
    # A forged token learns nothing: the ordinary confirm page.
    forged = intake_client.get("/api/intake/held/not-on-team", params={"token": f"{row.id}.3.bad"}).text
    assert "They are not on my team" in forged


def test_the_decliner_is_told_it_moved_without_the_new_name(world):
    email = _email()
    row = _hold(email)
    asyncio.run(held_requests.decline_held_request(row.id, "3"))
    world.sent.clear()
    asyncio.run(held_requests.repick_supervisor(row.id, email, "4", None))
    [to_steve] = [m for m in world.sent if m["to"] == ["stevef@ucsh.com"]]
    assert "moved to another supervisor" in to_steve["html"] and "Eduardo" not in to_steve["html"]


def test_release_continues_both_threads_and_says_who_approves(world, monkeypatch):
    numbers = _numbered_submit(world, monkeypatch, manager_id="3")   # Steve decides it
    email = _email()
    row = _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    world.sent.clear()
    asyncio.run(held_requests.release_held_requests(email))

    person = next(m for m in world.sent if m["to"] == [email])
    assert person["subject"] == held_requests.PERSON_SUBJECT
    assert _root(person["headers"]) == held_requests.thread_root_id(row.id, email)   # their own thread
    assert f"#{numbers[0]}" in person["html"] and "Steve Faith to approve" in person["html"]

    steve = next(m for m in world.sent if m["to"] == ["stevef@ucsh.com"])
    assert steve["subject"] == "Add Lee New to the Staff Directory"
    assert _root(steve["headers"]) == held_requests.thread_root_id(row.id, "stevef@ucsh.com")
    assert "waits for your approval" in steve["html"]
    assert not any("Your requests were submitted" == m["subject"] for m in world.sent)   # no separate email


def test_outbox_emails_about_the_released_request_join_its_threads(world, monkeypatch):
    from app.database import async_session

    numbers = _numbered_submit(world, monkeypatch)
    email = _email()
    row = _hold(email)
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    asyncio.run(held_requests.release_held_requests(email))
    request = types.SimpleNamespace(kind="leave", item_id=numbers[0])   # the RequestRecord the outbox holds

    async def thread(req, recipient):
        async with async_session() as session:
            return await held_requests.held_thread_for(session, req, recipient)

    subject, headers = asyncio.run(thread(request, email))
    assert subject == held_requests.PERSON_SUBJECT and _root(headers) == held_requests.thread_root_id(row.id, email)
    subject, _ = asyncio.run(thread(request, "SteveF@UCSH.com"))   # case does not matter
    assert subject == "Add Lee New to the Staff Directory"
    assert asyncio.run(thread(request, "someone@ucsh.com")) is None              # anyone else: ordinary email
    other_kind = types.SimpleNamespace(kind="overtime", item_id=numbers[0])
    assert asyncio.run(thread(other_kind, email)) is None                        # same number, other kind


def test_cancel_closes_the_supervisors_thread_only_while_they_hold_it(world):
    email = _email()
    row = _hold(email)
    world.sent.clear()
    asyncio.run(held_requests.cancel_held_request(row.id, "duplicate"))
    [closing] = world.sent
    assert closing["to"] == ["stevef@ucsh.com"] and "was cancelled" in closing["html"]
    assert _root(closing["headers"]) == held_requests.thread_root_id(row.id, "stevef@ucsh.com")

    # Declined first: Steve has nothing open, so a cancel tells him nothing.
    other = _hold(_email())
    asyncio.run(held_requests.decline_held_request(other.id, "3"))
    world.sent.clear()
    asyncio.run(held_requests.cancel_held_request(other.id, "duplicate"))
    assert world.sent == []
