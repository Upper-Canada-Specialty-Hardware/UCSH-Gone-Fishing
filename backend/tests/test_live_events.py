"""The live request feed, the indexed reads, the timeline and the version (#328-#332).

All of this runs on SQLite, so it exercises the polling path of the event bus
(no NOTIFY) and the Postgres-shaped indexed reads against the typed table. Each
test builds the one router it needs bare, the way test_admin_requests does, and
drives the database directly with ``run`` for the setup.
"""

import asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.models.request_change import RequestChange
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.routes import dashboard, events
from app.services.dashboard_tokens import generate_dashboard_token
from app.services.event_bus import EventBus, _current_max_id, fetch_events
from app.services.request_descriptions import compose_leave_title
from app.services.request_journal import append_event


def run(coro):
    """Run one coroutine to completion.

    Args:
        coro: The coroutine to run.

    Returns:
        Its result.
    """
    return asyncio.run(coro)


async def _reset() -> None:
    """Empty the request, event and change tables so each test starts clean."""
    async with async_session() as s:
        await s.execute(delete(RequestEvent))
        await s.execute(delete(RequestChange))
        await s.execute(delete(RequestRecord))
        await s.commit()


async def _add_request(
    *,
    kind: str = "leave",
    item_id: int,
    employee_id: str = "7",
    manager_id: str | None = "9",
    employee_name: str = "Pat Staff",
    status: str = "pending",
    stage: str = "Submitted",
    leave_type: str | None = "Vacation",
    start: date | None = date(2026, 11, 2),
    days: float | None = 2,
    extra: dict | None = None,
) -> int:
    """Insert one request row and return its surrogate id.

    Args:
        kind: The stored kind.
        item_id: The item number people see.
        employee_id: The employee's Staff Directory id.
        manager_id: The deciding manager's id, or None.
        employee_name: The employee's name.
        status: The lifecycle status (lower case).
        stage: The stage words.
        leave_type: The leave type (leave kind only).
        start: The start date.
        days: The cost in days.
        extra: SharePoint leftovers kept verbatim.

    Returns:
        The new row's surrogate id.
    """
    async with async_session() as s:
        row = RequestRecord(
            kind=kind, item_id=item_id, employee_id=employee_id, manager_id=manager_id,
            employee_name=employee_name, status=status, stage=stage, leave_type=leave_type,
            start_date=start, days=days, description="Family trip", extra=extra or {},
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row.id


async def _append(item_id: int, type_: str, channel: str, actor: str, key: str, kind: str = "leave") -> int | None:
    """Append one timeline event through the shared journal helper.

    Args:
        item_id: The request's item id.
        type_: The event type.
        channel: The channel.
        actor: Who caused it.
        key: The idempotency key.
        kind: The request kind.

    Returns:
        The new event id, or None when the key already existed.
    """
    async with async_session() as s:
        req = (await s.execute(
            select(RequestRecord).where(RequestRecord.kind == kind, RequestRecord.item_id == item_id)
        )).scalars().first()
        ev = await append_event(s, req, type_, channel, actor, {"note": "x"}, key)
        await s.commit()
        return ev.id if ev else None


# ----- the bus (polling path on SQLite) -----

def test_an_appended_event_reaches_a_subscriber():
    async def scenario():
        await _reset()
        await _add_request(item_id=101)
        bus = EventBus()
        bus._floor = await _current_max_id()                   # dispatch only what lands from here
        queue = bus.subscribe()
        await _append(101, "approved", "email_link", "Dave", "leave:101:approved:v1")
        count = await bus.poll_once()                          # the SQLite path, driven by hand
        return count, queue
    count, queue = run(scenario())
    assert count == 1
    payload = queue.get_nowait()
    assert payload["type"] == "approved"
    assert payload["item_id"] == 101
    assert payload["kind"] == "leave"
    assert payload["employee_id"] == "7"
    assert payload["stage"] == "Approved"                      # the request's stage moved with the event
    assert payload["status"] == "pending"                      # status is the approval path's job, not the journal's
    assert "body" not in payload                               # outbox bodies are never streamed


def test_the_stream_filters_by_role():
    emp = events._Viewer("employee", employee_id="7")
    mgr = events._Viewer("manager", manager_id="9", team_ids={"7", "8"})
    adm = events._Viewer("admin")
    own = {"employee_id": "7", "manager_id": "9"}
    teammate = {"employee_id": "8", "manager_id": "5"}
    outsider = {"employee_id": "99", "manager_id": "88"}
    routed_to_mgr = {"employee_id": "77", "manager_id": "9"}

    assert events._event_visible(own, emp) and not events._event_visible(teammate, emp)
    assert events._event_visible(own, mgr) and events._event_visible(teammate, mgr)
    assert events._event_visible(routed_to_mgr, mgr)           # routed to the manager, not on the team
    assert not events._event_visible(outsider, mgr)
    assert events._event_visible(outsider, adm)                # admin sees everything


def test_last_event_id_replay_returns_exactly_the_missed_events():
    async def scenario():
        await _reset()
        await _add_request(item_id=201)
        ids = [
            await _append(201, "submitted", "form", "Pat", "leave:201:submitted:v1"),
            await _append(201, "sent_to_manager", "system", None, "leave:201:sent_to_manager:v1"),
            await _append(201, "approved", "email_link", "Dave", "leave:201:approved:v1"),
        ]
        after_first = await fetch_events(ids[0])               # everything past the first event
        return ids, after_first
    ids, after_first = run(scenario())
    assert [p["id"] for p in after_first] == ids[1:]           # exactly the two missed, in order


async def _insert_event_with_id(request_id: int, item_id: int, event_id: int, key: str) -> None:
    """Insert one timeline event with an explicit id, to stage an out-of-order commit.

    Args:
        request_id: The request's surrogate id (the foreign key).
        item_id: The request's item number.
        event_id: The id to force (simulating a sequence value assigned earlier).
        key: The unique idempotency key.
    """
    async with async_session() as s:
        s.add(RequestEvent(
            id=event_id, request_id=request_id, kind="leave", item_id=item_id,
            employee_id="7", manager_id="9", type="approved", channel="system",
            actor="Dave", detail={}, idempotency_key=key,
        ))
        await s.commit()


def test_the_bus_delivers_an_event_that_commits_out_of_order():
    """A lower id that commits after a higher one is still dispatched, not skipped (finding 9)."""
    async def scenario():
        await _reset()
        rid = await _add_request(item_id=601)
        bus = EventBus()                                       # floor 0, nothing delivered yet
        queue = bus.subscribe()
        # Two ids already committed; id 1001 is still inside an open transaction.
        await _insert_event_with_id(rid, 601, 1000, "leave:601:e1000")
        await _insert_event_with_id(rid, 601, 1002, "leave:601:e1002")
        first = await bus.poll_once()                          # dispatches 1000 and 1002
        # The open transaction now commits id 1001, below the high-water mark 1002.
        await _insert_event_with_id(rid, 601, 1001, "leave:601:e1001")
        second = await bus.poll_once()                         # must pick up 1001, not skip it
        delivered = []
        while not queue.empty():
            delivered.append(queue.get_nowait()["id"])
        return first, second, delivered

    first, second, delivered = run(scenario())
    assert first == 2 and second == 1                          # 1000, 1002 then the late 1001
    assert set(delivered) == {1000, 1001, 1002}
    assert delivered.count(1001) == 1                          # delivered exactly once


# ----- the catch-up route and its role filter -----

@pytest.fixture
def events_client():
    """The events router alone, mounted bare without the admin guard."""
    app = FastAPI()
    app.include_router(events.router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


def _token(role: str, uid: str) -> dict:
    """A signed dashboard token as query parameters.

    Args:
        role: "employee", "manager" or "admin".
        uid: The user id.

    Returns:
        The token's query parameters.
    """
    return generate_dashboard_token(role, uid)


def test_catch_up_filters_for_an_employee(events_client):
    run(_reset())
    run(_add_request(item_id=301, employee_id="7"))
    run(_add_request(item_id=302, employee_id="8", manager_id="9"))
    run(_append(301, "approved", "email_link", "Dave", "leave:301:approved:v1"))
    run(_append(302, "approved", "email_link", "Dave", "leave:302:approved:v1"))

    resp = events_client.get("/api/dashboard/events", params={**_token("employee", "7"), "after": 0})
    assert resp.status_code == 200
    got = resp.json()["events"]
    assert got and all(p["employee_id"] == "7" for p in got)   # only the employee's own


def _admin_session_header(monkeypatch) -> dict:
    """Give the events route a known admin password and a valid session header.

    The admin feed needs the admin-dashboard password session, not a dashboard
    token (ruling 13), so a catch-up test signs in the way a real admin call does.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        ``{"X-Admin-Session": "<token>"}`` for a request's headers.
    """
    from app.services import admin_auth

    stored = "hashed-admin-password"

    async def fake_stored():
        return stored

    monkeypatch.setattr(events, "stored_password", fake_stored)
    return {"X-Admin-Session": admin_auth.make_session(stored)["token"]}


def test_catch_up_shows_everything_to_an_admin(events_client, monkeypatch):
    run(_reset())
    run(_add_request(item_id=311, employee_id="7"))
    run(_add_request(item_id=312, employee_id="8"))
    run(_append(311, "approved", "email_link", "Dave", "leave:311:approved:v1"))
    run(_append(312, "approved", "email_link", "Dave", "leave:312:approved:v1"))

    headers = _admin_session_header(monkeypatch)
    resp = events_client.get("/api/dashboard/events", params={"after": 0}, headers=headers)
    assert resp.status_code == 200
    employees = {p["employee_id"] for p in resp.json()["events"]}
    assert employees == {"7", "8"}


def test_catch_up_refuses_an_admin_role_token_without_the_session(events_client):
    """Ruling 13: an admin-role dashboard token alone no longer opens the admin feed."""
    run(_reset())
    run(_add_request(item_id=313, employee_id="7"))
    run(_append(313, "approved", "email_link", "Dave", "leave:313:approved:v1"))

    # A signed admin-role token, no admin session header: refused.
    resp = events_client.get("/api/dashboard/events", params={**_token("admin", "1"), "after": 0})
    assert resp.status_code == 401


def test_catch_up_filters_for_a_manager(events_client, monkeypatch):
    run(_reset())
    run(_add_request(item_id=321, employee_id="7", manager_id="5"))    # on the team
    run(_add_request(item_id=322, employee_id="99", manager_id="5"))   # outsider
    run(_append(321, "approved", "email_link", "Dave", "leave:321:approved:v1"))
    run(_append(322, "approved", "email_link", "Dave", "leave:322:approved:v1"))

    async def fake_team(_uid):
        return "9", {"7"}                                      # manager 9 manages employee 7
    monkeypatch.setattr(events, "_team_ids_for", fake_team)

    resp = events_client.get("/api/dashboard/events", params={**_token("manager", "9"), "after": 0})
    assert resp.status_code == 200
    employees = {p["employee_id"] for p in resp.json()["events"]}
    assert employees == {"7"}                                  # not the outsider


def test_catch_up_refuses_an_unauthenticated_caller(events_client):
    resp = events_client.get("/api/dashboard/events", params={"after": 0})
    assert resp.status_code == 401


def test_a_fresh_reader_starts_from_now_not_the_whole_history(events_client, monkeypatch):
    """No resume point means no replay: only where "now" is, to poll from."""
    run(_reset())
    run(_add_request(item_id=331, employee_id="7"))
    run(_append(331, "imported", "import", None, "import:leave:331"))
    run(_append(331, "approved", "email_link", "Dave", "leave:331:approved:v1"))

    headers = _admin_session_header(monkeypatch)
    resp = events_client.get("/api/dashboard/events", headers=headers)
    body = resp.json()
    assert resp.status_code == 200 and body["events"] == []    # history is not replayed
    assert body["last_id"] >= 2                                 # but the resume point is now


def test_the_admin_stream_takes_a_short_ticket_never_the_session(monkeypatch):
    """A ticket opens the feed; the 30-day session in a url does not; tickets expire."""
    from app.services import admin_auth

    stored = "hashed-password-value"
    ticket = admin_auth.make_stream_ticket(stored, now=1_000_000)
    assert admin_auth.stream_ticket_is_valid(ticket, stored, now=1_000_060)       # inside two minutes
    assert not admin_auth.stream_ticket_is_valid(ticket, stored, now=1_000_200)   # expired
    assert not admin_auth.stream_ticket_is_valid(ticket, "changed-password")      # password changed
    session = admin_auth.make_session(stored, now=1_000_000)["token"]
    assert not admin_auth.stream_ticket_is_valid(session, stored, now=1_000_060)  # a session is not a ticket

    async def fake_stored():
        return stored
    monkeypatch.setattr(events, "stored_password", fake_stored)
    app = FastAPI()
    app.include_router(events.router, prefix="/api/dashboard")
    client = TestClient(app, raise_server_exceptions=False)
    # The old query-string session no longer opens anything.
    resp = client.get("/api/dashboard/events/stream", params={"admin_session": session})
    assert resp.status_code == 401


# ----- the indexed reads (Postgres mode) return the SharePoint shape -----

@pytest.fixture
def dashboard_client(monkeypatch):
    """The dashboard router bare, with the Staff Directory lookups stubbed."""
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/dashboard")

    pat_staff = {"id": "7", "fields": {"Title": "Pat Staff", "AllManagers": [{"LookupValue": "Boss"}]}}
    sp_user_to_name = dashboard._PersonNames()
    sp_user_to_name[55] = "Pat Staff"                          # SubmittedTestLookupId 55 -> Pat Staff

    async def fake_lookups():
        return ({"pat staff": pat_staff}, {7: pat_staff}, sp_user_to_name, {"Boss": {"Pat Staff"}})

    async def no_taken_from(*_a, **_k):
        return None

    monkeypatch.setattr(dashboard, "_build_staff_lookups", fake_lookups)
    monkeypatch.setattr(dashboard, "attach_taken_from", no_taken_from)
    return TestClient(app, raise_server_exceptions=False)


def test_indexed_admin_read_matches_the_sharepoint_shape(dashboard_client, monkeypatch):
    run(_reset())
    # One imported-style leave row: the person blob and the assigned manager are
    # kept in extra, exactly as the importer preserves them.
    run(_add_request(
        item_id=400, employee_id="7", manager_id="9",
        extra={"SubmittedTestLookupId": 55, "ManagerLookupId": "9"},
    ))

    # The SharePoint item the whole-list path would return for the same request.
    sp_item = {"id": "400", "fields": {
        "SubmittedTestLookupId": 55, "ManagerLookupId": "9", "Status": "Pending",
        "LeaveType": "Vacation", "StartDate": "2026-11-02", "Days": 2,
        "EmployeeID": "7", "ManagerID": "9",
        "Title": compose_leave_title("Pat Staff", "Family trip"),
        "id": "400", "Created": "2026-10-16T00:00:00+00:00", "Modified": "2026-10-16T00:00:00+00:00",
    }}

    async def fake_list(list_id, **_k):
        return [sp_item] if list_id == settings.SP_LIST_LEAVE_REQUESTS else []

    # SharePoint path.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(dashboard.request_store, "get_list_items", fake_list)
    sp_resp = dashboard_client.get("/api/dashboard/admin/requests", params={"type": "leave"})
    assert sp_resp.status_code == 200
    sp_rows = sp_resp.json()["requests"]
    assert len(sp_rows) == 1
    sp_row = sp_rows[0]

    # Postgres path: indexed read of the typed table, no whole-list fetch.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    pg_resp = dashboard_client.get("/api/dashboard/admin/requests", params={"type": "leave"})
    assert pg_resp.status_code == 200
    pg_rows = pg_resp.json()["requests"]
    assert len(pg_rows) == 1
    pg_row = pg_rows[0]

    # Same shape, plus the new stage column.
    assert set(sp_row).issubset(set(pg_row))
    assert set(pg_row) - set(sp_row) == {"stage"}
    for key in ("id", "request_type", "Status", "StartDate", "Days", "LeaveType", "employee_name", "managers"):
        assert pg_row[key] == sp_row[key], key
    assert pg_row["employee_name"] == "Pat Staff"
    assert pg_row["managers"] == "Boss"
    assert pg_row["stage"] == "Submitted"


# ----- the timeline -----

@pytest.fixture
def timeline_client():
    """The events router bare, so the admin timeline needs no admin session."""
    app = FastAPI()
    app.include_router(events.router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


def test_admin_timeline_returns_lines_and_the_field_history(timeline_client):
    run(_reset())
    run(_add_request(item_id=500, employee_id="7"))
    run(_append(500, "submitted", "form", "Pat", "leave:500:submitted:v1"))
    run(_append(500, "approved", "email_link", "Dave", "leave:500:approved:v1"))

    async def add_change():
        async with async_session() as s:
            req = (await s.execute(
                select(RequestRecord).where(RequestRecord.item_id == 500)
            )).scalars().first()
            s.add(RequestChange(
                request_id=req.id, field="Days", old_value=1, new_value=2,
                actor="admin", channel="admin_dashboard", reason="fixed",
            ))
            await s.commit()
    run(add_change())

    resp = timeline_client.get("/api/dashboard/admin/requests/leave/500/timeline")
    assert resp.status_code == 200
    body = resp.json()
    lines = [e["line"] for e in body["events"]]
    assert "Approved by email by Dave" in lines
    assert len(body["changes"]) == 1
    assert body["changes"][0]["field"] == "Days"
    assert body["changes"][0]["new"] == 2


def test_my_timeline_hides_the_field_history_and_guards_by_owner(timeline_client):
    run(_reset())
    run(_add_request(item_id=510, employee_id="7"))
    run(_append(510, "approved", "email_link", "Dave", "leave:510:approved:v1"))

    mine = timeline_client.get(
        "/api/dashboard/me/requests/leave/510/timeline", params=_token("employee", "7")
    )
    assert mine.status_code == 200
    assert mine.json()["changes"] == []                        # field history is admin-only
    assert mine.json()["events"]

    other = timeline_client.get(
        "/api/dashboard/me/requests/leave/510/timeline", params=_token("employee", "8")
    )
    assert other.status_code == 404                            # not this employee's request


# ----- the version -----

def test_version_endpoint_reports_the_build(events_client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    resp = events_client.get("/api/dashboard/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": "1.2.3"}


def test_every_response_carries_the_app_version_header(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    from app.main import app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dashboard/version")
    assert resp.headers.get("X-App-Version") == "9.9.9"
