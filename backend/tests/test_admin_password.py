"""The admin dashboard password and the server-side session check (#228).

The password is one row in admin_password. A developer's plain value works
and is hashed at the next sign-in; a deleted row locks the dashboard rather
than falling back to 123; changing the password ends every session.
"""

import asyncio

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import async_session
from app.models.admin_password import AdminPassword
from app.routes import admin_auth as admin_auth_routes
from app.routes.dashboard import router as dashboard_router
from app.services import admin_auth


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


async def _set_row(value):
    """Replace the table with one row holding value, or no row for None."""
    async with async_session() as session:
        await session.execute(delete(AdminPassword))
        if value is not None:
            session.add(AdminPassword(password=value))
        await session.commit()


async def _row_value():
    """The stored value, or None."""
    async with async_session() as session:
        row = (await session.execute(select(AdminPassword))).scalar_one_or_none()
        return row.password if row else None


@pytest.fixture(autouse=True)
def seeded():
    """Every test starts like a fresh deploy: one row, plain 123, no failed tries."""
    admin_auth._failures.clear()
    run(_set_row("123"))
    yield
    run(_set_row(None))
    admin_auth._failures.clear()


@pytest.fixture
def client():
    """The admin routes wired the way main.py wires them."""
    app = FastAPI()
    guard = [Depends(admin_auth.admin_guard)]
    app.include_router(dashboard_router, prefix="/api/dashboard", dependencies=guard)
    app.include_router(admin_auth_routes.router, prefix="/api/dashboard", dependencies=guard)
    app.include_router(admin_auth_routes.public_router, prefix="/api/admin-auth")
    return TestClient(app)


def _sign_in(client, password):
    """POST the sign-in form."""
    return client.post("/api/admin-auth/sign-in", json={"password": password})


def _headers(token):
    """The header the page sends on every admin call."""
    return {admin_auth.SESSION_HEADER: token}


def test_every_admin_route_in_the_app_is_guarded():
    from app.main import app
    admin_routes = [r for r in app.routes if getattr(r, "path", "").startswith(admin_auth.ADMIN_PATH)]
    assert len(admin_routes) > 40                                       # the whole admin dashboard
    for route in admin_routes:
        calls = [d.call for d in route.dependant.dependencies]
        assert admin_auth.admin_guard in calls, route.path


def test_admin_calls_are_refused_without_a_session(client):
    response = client.get("/api/dashboard/admin/held-requests")
    assert response.status_code == 401
    bad = client.get("/api/dashboard/admin/held-requests", headers=_headers("9999999999.forged"))
    assert bad.status_code == 401


def test_staff_routes_are_not_touched_by_the_guard(client):
    assert client.get("/api/dashboard/config").status_code == 200


def test_the_seeded_password_signs_in_and_is_hashed_on_the_way(client):
    response = _sign_in(client, "123")
    assert response.status_code == 200
    stored = run(_row_value())
    assert admin_auth.is_hashed(stored) and "123" not in stored.split("$")[-1]
    ok = client.get("/api/dashboard/admin/held-requests", headers=_headers(response.json()["token"]))
    assert ok.status_code == 200


def test_a_wrong_password_is_refused(client):
    response = _sign_in(client, "124")
    assert response.status_code == 401 and "not right" in response.json()["detail"]


def test_a_developer_can_set_any_plain_value_in_the_table(client):
    run(_set_row("summer-2027"))
    assert _sign_in(client, "123").status_code == 401
    assert _sign_in(client, "summer-2027").status_code == 200


def test_a_deleted_row_locks_the_dashboard_instead_of_going_back_to_123(client):
    token = _sign_in(client, "123").json()["token"]
    run(_set_row(None))
    assert _sign_in(client, "123").status_code == 403
    assert client.get("/api/dashboard/admin/held-requests", headers=_headers(token)).status_code == 401
    assert run(_row_value()) is None                                    # nothing re-seeded it


def test_a_database_change_signs_everyone_out(client):
    token = _sign_in(client, "123").json()["token"]
    run(_set_row("new-one"))
    assert client.get("/api/dashboard/admin/held-requests", headers=_headers(token)).status_code == 401


def test_changing_the_password_from_the_dashboard(client):
    old = _sign_in(client, "123").json()["token"]
    other_device = _sign_in(client, "123").json()["token"]
    wrong = client.put("/api/dashboard/admin/password", json={"current": "nope", "new": "x"}, headers=_headers(old))
    assert wrong.status_code == 400                                     # not 401: still signed in
    changed = client.put("/api/dashboard/admin/password", json={"current": "123", "new": "hr-picked"}, headers=_headers(old))
    assert changed.status_code == 200
    fresh = changed.json()["token"]
    assert client.get("/api/dashboard/admin/held-requests", headers=_headers(fresh)).status_code == 200
    assert client.get("/api/dashboard/admin/held-requests", headers=_headers(other_device)).status_code == 401
    assert _sign_in(client, "123").status_code == 401
    assert _sign_in(client, "hr-picked").status_code == 200


def test_changing_needs_a_session(client):
    response = client.put("/api/dashboard/admin/password", json={"current": "123", "new": "x"})
    assert response.status_code == 401


def test_an_empty_new_password_is_refused(client):
    token = _sign_in(client, "123").json()["token"]
    response = client.put("/api/dashboard/admin/password", json={"current": "123", "new": "  "}, headers=_headers(token))
    assert response.status_code == 400


def test_too_many_wrong_passwords_are_slowed_down(client):
    for _ in range(admin_auth.MAX_FAILURES):
        assert _sign_in(client, "wrong").status_code == 401
    assert _sign_in(client, "123").status_code == 429                  # even the right one, for now


def test_an_expired_session_is_refused():
    stored = admin_auth.hash_password("123", salt=b"0" * 16)
    session = admin_auth.make_session(stored, now=1_000)
    assert admin_auth.session_is_valid(session["token"], stored, now=1_000 + 60)
    assert not admin_auth.session_is_valid(session["token"], stored, now=1_000 + 31 * 86400)
