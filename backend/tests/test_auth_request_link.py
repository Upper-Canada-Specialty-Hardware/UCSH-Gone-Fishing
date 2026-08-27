"""The self-service sign-in link must be useful without being an oracle.

`POST /api/auth/request-link` emails a signed dashboard link to a known @ucsh
address. What is worth pinning: it never reveals whether an address is on file
(same response either way, no send for an unknown one), it never mints an admin
link, and it is rate-limited so it cannot be used to bomb an address.

The employee lookup and the mailer are faked, so nothing here touches SharePoint
or SMTP2GO.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import auth as auth_route
from app.routes.auth import router as auth_router


@pytest.fixture
def sent():
    """Capture what would have been emailed, returned as a mutable list."""
    return []


@pytest.fixture
def client(monkeypatch, sent):
    """A TestClient with the employee lookup, roles, and mailer all faked.

    By default the email resolves to an employee who is also a manager and an
    admin, so a single fixture exercises role filtering (admin must be dropped).
    """
    async def _get_by_email(email):
        if email == "jane@ucsh.com":
            return {"id": "12", "fields": {"Title": "Jane Doe"}}
        return None

    async def _get_roles(employee):
        return ["employee", "manager", "admin"]  # admin present -> must be filtered out

    async def _send_email(to, subject, html_body, **kwargs):
        sent.append({"to": to, "subject": subject, "html": html_body})

    monkeypatch.setattr(auth_route, "get_employee_by_email", _get_by_email)
    monkeypatch.setattr(auth_route, "get_employee_roles", _get_roles)
    monkeypatch.setattr(auth_route, "send_email", _send_email)
    monkeypatch.setattr(auth_route, "_hits", {})  # reset the rate-limit window per test

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    return TestClient(app, raise_server_exceptions=False)


def _post(client, email):
    return client.post("/api/auth/request-link", json={"email": email})


def test_a_known_email_is_sent_a_link(client, sent):
    resp = _post(client, "jane@ucsh.com")
    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["to"] == ["jane@ucsh.com"]


def test_admin_is_never_offered_as_a_self_service_link(client, sent):
    # Jane is an admin, but the email must only carry employee + manager links.
    _post(client, "jane@ucsh.com")
    html = sent[0]["html"]
    assert "manager dashboard" in html
    assert "employee dashboard" in html
    assert "admin dashboard" not in html


def test_an_unknown_email_gets_the_same_response_and_no_send(client, sent):
    resp = _post(client, "stranger@example.invalid")
    assert resp.status_code == 200
    # Same body a known email gets (the shared generic response): not an oracle.
    assert resp.json() == auth_route._GENERIC_RESPONSE
    assert sent == []  # nothing emailed for the unknown address


def test_email_is_case_insensitive(client, sent):
    _post(client, "JANE@UCSH.com")
    assert len(sent) == 1  # normalised before lookup


def test_repeated_requests_are_rate_limited(client, sent):
    # Window allows 3; the 4th must not send, but still returns the generic body.
    for _ in range(3):
        _post(client, "jane@ucsh.com")
    resp = _post(client, "jane@ucsh.com")
    assert resp.status_code == 200
    assert len(sent) == 3  # the 4th was suppressed
