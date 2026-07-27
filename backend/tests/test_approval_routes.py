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


def _url(request_type="leave", action="approve", request_id="3402", mgr="42", version=1):
    token = _sign(request_type, request_id, action, mgr, str(NO_EXPIRY), version)
    return (
        f"/api/{request_type}/{action}/{request_id}"
        f"?token={token}&mgr={mgr}&exp={NO_EXPIRY}&v={version}"
    )


@pytest.fixture
def processing_on(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)


@pytest.mark.parametrize("request_type", ["leave", "overtime", "carryover-payout"])
@pytest.mark.parametrize("action", ["approve", "reject"])
def test_confirm_page_renders(client, monkeypatch, processing_on, request_type, action):
    """The page a manager lands on when clicking an approval link."""
    async def _same_version(list_id, request_id):
        return 1

    monkeypatch.setattr(approval_route, "get_current_version", _same_version)

    resp = client.get(_url(request_type=request_type, action=action))

    assert resp.status_code == 200, resp.text
    assert "3402" in resp.text


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


def test_unknown_request_type_renders_error_page(client, processing_on):
    resp = client.get(_url(request_type="bogus"))

    assert resp.status_code == 200, resp.text
