"""Approval link pages must render, not 500.

Regression guard for the Starlette 1.x TemplateResponse signature change:
the legacy `TemplateResponse(name, {"request": request, ...})` order was
removed, and passing it silently treats the context dict as the template
name -> TypeError -> every manager approval link 500s. These tests fail
against the old call order under starlette>=1.0 and pass under the new one.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import approval as approval_route
from app.routes.approval import router as approval_router
from app.services.approval_links import NO_EXPIRY, _sign


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(approval_router, prefix="/api")
    # raise_server_exceptions=False so a route blowing up surfaces as a 500
    # response (what the manager actually sees) instead of re-raising.
    return TestClient(app, raise_server_exceptions=False)


def _path(request_type="leave", action="approve", request_id="3402"):
    return f"/api/{request_type}/{action}/{request_id}"


def _form(request_type="leave", action="approve", request_id="3402", mgr="42", version=1):
    token = _sign(request_type, request_id, action, mgr, str(NO_EXPIRY), version)
    return {"token": token, "mgr": mgr, "exp": str(NO_EXPIRY), "v": str(version)}


def _url(request_type="leave", action="approve", request_id="3402", mgr="42", version=1):
    fields = _form(request_type, action, request_id, mgr, version)
    return (
        f"{_path(request_type, action, request_id)}"
        f"?token={fields['token']}&mgr={mgr}&exp={NO_EXPIRY}&v={version}"
    )


@pytest.fixture
def processing_on(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)


@pytest.fixture
def version_matches(monkeypatch):
    """Link version == current version, so validation reaches the real handler."""
    async def _same_version(list_id, request_id):
        return 1

    monkeypatch.setattr(approval_route, "get_current_version", _same_version)


@pytest.mark.parametrize("request_type", ["leave", "overtime", "carryover-payout"])
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_confirm_page_renders(client, processing_on, version_matches, request_type, action):
    """The page a manager lands on when clicking an approval link."""
    resp = client.get(_url(request_type=request_type, action=action))

    assert resp.status_code == 200, resp.text
    assert "3402" in resp.text
    assert "Nothing has been processed yet" in resp.text


def test_reporting_mode_renders_error_page(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)

    resp = client.get(_url())

    assert resp.status_code == 200, resp.text
    assert "reporting-only mode" in resp.text


def test_invalid_token_renders_error_page(client, processing_on):
    resp = client.get(
        f"/api/leave/approve/3402?token=deadbeef&mgr=42&exp={NO_EXPIRY}&v=1"
    )

    assert resp.status_code == 200, resp.text
    assert "Invalid token" in resp.text


def test_stale_link_renders_outdated_page(client, monkeypatch, processing_on):
    async def _newer_version(list_id, request_id):
        return 7

    monkeypatch.setattr(approval_route, "get_current_version", _newer_version)

    resp = client.get(_url(version=1))

    assert resp.status_code == 200, resp.text
    # Every page in this module returns 200, so the body is the only thing that
    # proves the outdated page rendered rather than the confirm page.
    assert "more recent email" in resp.text


def test_unknown_request_type_renders_error_page(client, processing_on):
    resp = client.get(_url(request_type="bogus"))

    assert resp.status_code == 200, resp.text
    assert "Invalid request type or action" in resp.text


# ----- POST: the pages a manager sees after confirming -----


@pytest.mark.parametrize("request_type", ["leave", "overtime", "carryover-payout"])
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_action_result_page_renders(client, monkeypatch, processing_on, version_matches, request_type, action):
    """approval_success.html / approval_rejected.html - the highest-value pages."""
    async def _handler(request_id, mgr):
        return {"status": "approved" if action == "approve" else "rejected"}

    monkeypatch.setitem(approval_route.HANDLERS, (request_type, action), _handler)

    resp = client.post(_path(request_type, action), data=_form(request_type, action))

    assert resp.status_code == 200, resp.text
    assert "3402" in resp.text
    assert ("approved" if action == "approve" else "rejected") in resp.text.lower()


def test_handler_error_renders_error_page(client, monkeypatch, processing_on, version_matches):
    async def _handler(request_id, mgr):
        return {"error": "Already processed"}

    monkeypatch.setitem(approval_route.HANDLERS, ("leave", "approve"), _handler)

    resp = client.post(_path(), data=_form())

    assert resp.status_code == 200, resp.text
    assert "Already processed" in resp.text


def test_handler_exception_renders_error_page(client, monkeypatch, processing_on, version_matches):
    async def _handler(request_id, mgr):
        raise RuntimeError("SharePoint exploded")

    monkeypatch.setitem(approval_route.HANDLERS, ("leave", "approve"), _handler)

    resp = client.post(_path(), data=_form())

    assert resp.status_code == 200, resp.text
    assert "SharePoint exploded" in resp.text


def test_status_page_renders():
    """health.py renders status.html through the same templating call."""
    from app.routes.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    resp = TestClient(app, raise_server_exceptions=False).get("/status")

    assert resp.status_code == 200, resp.text
    assert "<html" in resp.text.lower()
