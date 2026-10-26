"""Held new hires on the manager dashboard, the follow-after-release sign-in, and
the public "Not on my team" page (#481, #482, #483).

Covers:
- #481: the list of held new hires waiting on one supervisor, the GET /team/held
  route (managers only), and the dashboard's "not on my team" POST (supervisor
  only, 404/409 as fits).
- #482: the released request's status token on the held status answer, and
  POST /held/sign-in (released within 7 days signs in; refused otherwise).
- #483: the JSON "not on my team" info and action endpoints, and that the email
  link now points at the public site.

Like the other held tests this stubs the Staff Directory, email and request
creation so nothing real is called.
"""
import asyncio
import itertools
import time
import types
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models.mixins import utcnow
from app.routes import intake, team_manage
from app.services import dashboard_tokens, email_codes, held_requests, leave_requests

_counter = itertools.count()


def _email() -> str:
    """An address no earlier run has used (the test database keeps rows)."""
    return f"newhire{next(_counter)}-{time.time_ns()}@gmail.com"


# Two supervisors in one department, as in test_held_requests_one_at_a_time.
STEVE = {"id": "3", "fields": {"Title": "Steve Faith", "EmailAddress": "stevef@ucsh.com",
                               "Department": "Project Management"}}
EDUARDO = {"id": "4", "fields": {"Title": "Eduardo Del Sarto", "EmailAddress": "eduardod@ucsh.com",
                                 "Department": "Project Management"}}
VACATION = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"}


@pytest.fixture
def world(monkeypatch):
    """Stub the Staff Directory, email and request creation (held tests' shape)."""
    class World:
        sent: list = []
        staff_by_email: dict = {}
        created: list = []
    w = World()
    w.sent, w.staff_by_email, w.created = [], {}, []
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)

    async def staff_list(list_id, **kwargs):
        return [STEVE, EDUARDO]

    async def by_email(email):
        return w.staff_by_email.get((email or "").strip().lower())

    async def by_name(name):
        return {STEVE["fields"]["Title"]: STEVE, EDUARDO["fields"]["Title"]: EDUARDO}.get(name)

    async def fake_send(to, subject, html_body, **kwargs):
        w.sent.append({"to": to, "subject": subject, "html": html_body, "headers": kwargs.get("headers")})

    async def fake_submit(request_type, form, employee, source, **kwargs):
        w.created.append((request_type, employee["id"], source))
        return {"id": f"7{len(w.created)}", "fields": {}}

    async def lookup(email):
        return 12

    monkeypatch.setattr(sp_client, "get_list_items", staff_list)
    monkeypatch.setattr(held_requests, "get_employee_by_email", by_email)
    monkeypatch.setattr(held_requests, "get_employee_by_name", by_name)
    monkeypatch.setattr(held_requests, "send_email", fake_send)
    monkeypatch.setattr(held_requests, "submit_request", fake_submit)
    monkeypatch.setattr(leave_requests, "_resolve_user_lookup_id", lookup)
    monkeypatch.setattr(intake, "get_employee_by_email", by_email)
    return w


def _hold(email, supervisor_id="3"):
    """Hold a new hire's vacation request to one supervisor (Steve by default)."""
    return asyncio.run(held_requests.hold_request(
        email, name="Lee New", department="Project Management",
        supervisor_id=supervisor_id, request_type="leave", form=VACATION,
    ))


# ----- #481: the list service and the team routes -----

def test_held_waiting_on_supervisor_is_only_their_open_rows(world):
    mine = _hold(_email(), supervisor_id="3")              # held, waiting on Steve
    _hold(_email(), supervisor_id="4")                     # someone else's: not Steve's
    declined = _hold(_email(), supervisor_id="3")
    asyncio.run(held_requests.decline_held_request(declined.id, "3"))   # now not_on_team: left Steve

    rows = asyncio.run(held_requests.held_waiting_on_supervisor("3"))
    ids = {r["id"] for r in rows}
    assert mine.id in ids                                  # the open one shows
    assert declined.id not in ids                          # a declined row no longer waits on them
    row = next(r for r in rows if r["id"] == mine.id)
    assert row["name"] == "Lee New" and row["status"] == "held"
    assert row["summary"] == "Vacation, 2026-11-02 to 2026-11-03" and row["department"] == "Project Management"


def test_held_waiting_on_supervisor_reports_the_single_name_match(world):
    # existing_id is the one active staff record sharing the row's name, matched
    # by id on the server, or null when none or more than one does (#406).
    unmatched = _hold(_email(), supervisor_id="3")             # "Lee New": no record by that name
    matched = asyncio.run(held_requests.hold_request(
        _email(), name="Eduardo Del Sarto", department="Project Management",
        supervisor_id="3", request_type="leave", form=VACATION))   # matches EDUARDO (id 4)

    rows = {r["id"]: r for r in asyncio.run(held_requests.held_waiting_on_supervisor("3"))}
    assert rows[unmatched.id]["existing_id"] is None           # no staff record by that name
    assert rows[matched.id]["existing_id"] == "4"              # exactly one match, by id


@pytest.fixture
def team_client(world, monkeypatch):
    """A client for the team router, with the caller's own record stubbed."""
    records = {"3": STEVE, "4": EDUARDO}

    async def by_id(uid):
        return records.get(str(uid))

    monkeypatch.setattr(team_manage, "get_employee_by_id", by_id)
    app = FastAPI()
    app.include_router(team_manage.router, prefix="/api/dashboard")
    return TestClient(app)


def _as(role, uid):
    """Signed dashboard token query parameters for one role and id."""
    return dashboard_tokens.generate_dashboard_token(role, uid)


def test_team_held_lists_only_the_signed_in_supervisors_rows(team_client, world):
    mine = _hold(_email(), supervisor_id="3")
    _hold(_email(), supervisor_id="4")
    res = team_client.get("/api/dashboard/team/held", params=_as("manager", "3"))
    assert res.status_code == 200
    ids = {r["id"] for r in res.json()["held"]}
    assert mine.id in ids


def test_team_held_is_refused_for_an_employee(team_client, world):
    assert team_client.get("/api/dashboard/team/held", params=_as("employee", "3")).status_code == 403


def test_team_not_on_team_declines_only_for_the_holder(team_client, world):
    row = _hold(_email(), supervisor_id="3")
    # Another supervisor cannot hand back a row that is not theirs.
    other = team_client.post(f"/api/dashboard/team/held/{row.id}/not-on-team", params=_as("manager", "4"))
    assert other.status_code == 409
    # The holder hands it back; the person is asked to pick again.
    ok = team_client.post(f"/api/dashboard/team/held/{row.id}/not-on-team", params=_as("manager", "3"))
    assert ok.status_code == 200 and ok.json()["status"] == "declined"
    assert asyncio.run(held_requests.open_held_request_for(email=row.email)).status == "not_on_team"


def test_team_not_on_team_404_for_an_unknown_row(team_client, world):
    assert team_client.post("/api/dashboard/team/held/99999999/not-on-team",
                            params=_as("manager", "3")).status_code == 404


# ----- #482: the status token after release, and the sign-in -----

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


def _release(world, email):
    """Put ``email`` on staff and release their held request (what Add Employee runs)."""
    world.staff_by_email[email] = {"id": "8", "fields": {"Title": "Lee New", "EmailAddress": email}}
    return asyncio.run(held_requests.release_held_requests(email))


def test_released_status_answer_carries_the_request_token_on_postgres(intake_client, world, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")   # the Postgres-only gate the submit uses
    email = _email()
    row = _hold(email)
    token = held_requests.held_status_token(row.id)
    _release(world, email)
    data = intake_client.get("/api/intake/held/status", params={"token": token}).json()
    assert data["status"] == "sent"
    # The token names the released request, so the page follows it to the decision.
    assert held_requests.held_id_from_status_token  # sanity: the module is wired
    from app.services import request_status
    assert request_status.request_ref_from_status_token(data["request_status_token"]) is not None


def test_released_status_answer_has_no_token_off_postgres(intake_client, world):
    email = _email()
    row = _hold(email)
    token = held_requests.held_status_token(row.id)
    _release(world, email)
    data = intake_client.get("/api/intake/held/status", params={"token": token}).json()
    assert data["status"] == "sent" and "request_status_token" not in data   # SharePoint path: nothing to follow


def test_sign_in_works_for_a_recent_release(intake_client, world):
    email = _email()
    row = _hold(email)
    _release(world, email)
    token = held_requests.held_status_token(row.id)
    res = intake_client.post("/api/intake/held/sign-in", json={"status_token": token})
    assert res.status_code == 200
    body = res.json()
    assert body["uid"] == "8" and body["email"] == email and body["token"]   # a normal staff session
    assert "request_status_token" in body


def test_sign_in_is_refused_while_still_held(intake_client, world):
    row = _hold(_email())
    token = held_requests.held_status_token(row.id)
    assert intake_client.post("/api/intake/held/sign-in", json={"status_token": token}).status_code == 409


def test_sign_in_is_refused_after_the_window(intake_client, world):
    email = _email()
    row = _hold(email)
    _release(world, email)

    async def age_it():
        async with async_session() as session:
            stored = await session.get(held_requests.HeldRequest, row.id)
            stored.released_at = utcnow() - timedelta(days=held_requests.RELEASE_SIGN_IN_DAYS + 1)
            await session.commit()
    asyncio.run(age_it())
    token = held_requests.held_status_token(row.id)
    assert intake_client.post("/api/intake/held/sign-in", json={"status_token": token}).status_code == 409


def test_sign_in_404_when_the_staff_record_is_gone(intake_client, world):
    email = _email()
    row = _hold(email)
    _release(world, email)
    world.staff_by_email.clear()                           # record vanished after the release
    token = held_requests.held_status_token(row.id)
    assert intake_client.post("/api/intake/held/sign-in", json={"status_token": token}).status_code == 404


def test_sign_in_404_for_a_forged_token(intake_client, world):
    assert intake_client.post("/api/intake/held/sign-in", json={"status_token": "nonsense"}).status_code == 404


# ----- #483: the public "Not on my team" JSON endpoints and the link -----

def test_not_on_team_info_shows_the_open_request(intake_client, world):
    email = _email()
    row = _hold(email)
    token = held_requests.not_on_team_token(row.id, "3")
    info = intake_client.get("/api/intake/held/not-on-team/info", params={"token": token}).json()
    assert info["state"] == "open" and info["name"] == "Lee New"
    assert info["supervisor_name"] == "Steve Faith"
    assert info["summary"] == "Vacation, 2026-11-02 to 2026-11-03"


def test_not_on_team_info_is_invalid_for_a_forged_link(intake_client, world):
    info = intake_client.get("/api/intake/held/not-on-team/info", params={"token": "5.3.bad"}).json()
    assert info["state"] == "invalid"


def test_not_on_team_confirm_declines_then_is_idempotent(intake_client, world):
    email = _email()
    row = _hold(email)
    token = held_requests.not_on_team_token(row.id, "3")
    done = intake_client.post("/api/intake/held/not-on-team/confirm", params={"token": token}).json()
    assert done["state"] == "done" and done["name"] == "Lee New"
    assert asyncio.run(held_requests.open_held_request_for(email=email)).status == "not_on_team"
    # Opened again, the same link says it already moved on, not a second decline.
    again = intake_client.get("/api/intake/held/not-on-team/info", params={"token": token}).json()
    assert again["state"] == "declined"


def test_not_on_team_confirm_is_refused_while_processing_is_off(intake_client, world, monkeypatch):
    row = _hold(_email())
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    token = held_requests.not_on_team_token(row.id, "3")
    assert intake_client.post("/api/intake/held/not-on-team/confirm", params={"token": token}).status_code == 503


def test_the_not_on_team_email_link_points_at_the_public_site(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_FRONTEND_URL", "https://ucshoutofoffice.example.com/")
    link = held_requests.not_on_team_link(5, "3")
    assert link.startswith("https://ucshoutofoffice.example.com/#/held/not-on-team?token=")
    assert "/api/intake/" not in link                      # no longer a backend link (#483)
