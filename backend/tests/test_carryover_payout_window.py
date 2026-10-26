"""The admin's dates for carry-over and payout requests (#176).

No dates by default, which means closed. Outside the dates, every way staff
send a new request refuses a carry-over or payout one before anything is
created, so nothing reaches a manager and no email goes out. Leave and
overtime are never affected.
"""

import asyncio
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.app_setting import AppSetting
from app.routes import intake, self_service
from app.routes.dashboard import router as dashboard_router
from app.services import app_settings, held_requests, request_intake
from app.services.dashboard_tokens import generate_dashboard_token

PAT = {"id": "7", "fields": {"Title": "Pat Q Staff", "EmailAddress": "pat@ucsh.com"}}
PAYOUT = {"type_of_request": "Payout", "days": 2}


def around_today():
    """A window that contains today in Toronto."""
    today = app_settings.toronto_today()
    return today - timedelta(days=3), today + timedelta(days=3)


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_stored_setting():
    """Start and end every test with the switch never set (the default)."""
    async def wipe():
        async with async_session() as session:
            await session.execute(delete(AppSetting))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


@pytest.fixture
def client(monkeypatch):
    """The request-page, intake and admin routes, with request creation recorded."""
    app = FastAPI()
    app.include_router(self_service.router, prefix="/api/dashboard")
    app.include_router(intake.router, prefix="/api/intake")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    test_client = TestClient(app)
    test_client.created = []
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(settings, "REQUEST_EMAIL_COLUMNS_ENABLED", True)

    async def by_id(item_id):
        return PAT

    async def create(form_data, email, source=None, notify=True, employee_id=None):
        test_client.created.append(form_data)
        return {"id": "500"}

    monkeypatch.setattr(self_service, "get_employee_by_id", by_id)
    monkeypatch.setattr(request_intake, "process_new_carryover_payout", create)
    monkeypatch.setattr(request_intake, "process_new_leave_request", create)

    # A leave submission now reads the leave list for an overlap clash (#446);
    # an empty list means no clash, so leave still goes through here untouched.
    async def no_leave(*a, **k):
        return []

    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", no_leave)
    return test_client


def _submit(client, request_type, body):
    """POST a form as Pat, signed in."""
    token = generate_dashboard_token("employee", "7")
    return client.post(f"/api/dashboard/me/requests/{request_type}", params=token, json=body)


def test_closed_by_default_and_open_only_inside_the_dates():
    assert run(app_settings.carryover_payout_window()) is None             # no row: no dates
    assert run(app_settings.carryover_payout_open()) is False
    run(app_settings.set_carryover_payout_window(date(2026, 11, 1), date(2026, 11, 30)))
    assert run(app_settings.carryover_payout_open(date(2026, 10, 31))) is False   # day before
    assert run(app_settings.carryover_payout_open(date(2026, 11, 1))) is True     # first day counts
    assert run(app_settings.carryover_payout_open(date(2026, 11, 30))) is True    # last day counts
    assert run(app_settings.carryover_payout_open(date(2026, 12, 1))) is False    # day after
    run(app_settings.set_carryover_payout_window(None, None))                     # cleared
    assert run(app_settings.carryover_payout_window()) is None


def test_half_a_window_or_a_backwards_one_is_refused():
    with pytest.raises(ValueError):
        run(app_settings.set_carryover_payout_window(date(2026, 11, 1), None))
    with pytest.raises(ValueError):
        run(app_settings.set_carryover_payout_window(date(2026, 11, 30), date(2026, 11, 1)))


def test_an_unknown_setting_cannot_be_created():
    with pytest.raises(KeyError):
        run(app_settings.set_setting("carryover_open", True))


def test_a_closed_payout_request_is_refused_before_anything_is_created(client):
    response = _submit(client, "carryover-payout", PAYOUT)
    assert response.status_code == 400 and "not open right now" in response.json()["detail"]
    assert client.created == []                                         # no item, so no manager email


def test_before_the_dates_it_says_when_they_open(client):
    today = app_settings.toronto_today()
    run(app_settings.set_carryover_payout_window(today + timedelta(days=5), today + timedelta(days=9)))
    response = _submit(client, "carryover-payout", PAYOUT)
    assert response.status_code == 400 and "open on" in response.json()["detail"]


def test_an_open_payout_request_is_created(client):
    run(app_settings.set_carryover_payout_window(*around_today()))
    assert _submit(client, "carryover-payout", PAYOUT).status_code == 200
    assert client.created == [{"type_of_request": "Payout", "days": 2.0}]


def test_leave_is_never_affected(client):
    body = {"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"}
    assert _submit(client, "leave", body).status_code == 200


def test_a_new_hire_cannot_hold_one_while_closed():
    with pytest.raises(request_intake.RequestTypeClosed):
        run(held_requests.hold_request(
            "newhire@gmail.com", "Lee New", "Shop", "3", "carryover-payout", PAYOUT,   # refused before the department is looked up
        ))


def test_the_request_page_and_the_admin_see_the_same_dates(client):
    page = client.get("/api/intake/request-types").json()
    assert page["carryover_payout_open"] is False and page["carryover_payout_window"] is None
    start, end = around_today()
    saved = client.put("/api/dashboard/admin/carryover-payout",
                       json={"start": start.isoformat(), "end": end.isoformat()}).json()
    assert saved["open"] is True and saved["start"] == start.isoformat()
    page = client.get("/api/intake/request-types").json()
    assert page["carryover_payout_open"] is True
    assert page["carryover_payout_window"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert client.get("/api/dashboard/admin/carryover-payout").json()["end"] == end.isoformat()


def test_the_admin_gets_a_readable_400_for_bad_dates(client):
    r = client.put("/api/dashboard/admin/carryover-payout", json={"start": "2026-11-30", "end": "2026-11-01"})
    assert r.status_code == 400 and "before the start" in r.json()["detail"]
