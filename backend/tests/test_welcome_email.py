"""The welcome email after Add Employee (#227)."""

import asyncio

from app.config import settings
from app.routes import dashboard
from app.services import welcome_email

RECORD = {"id": "812", "fields": {"Title": "Lee New", "EmailAddress": "lee.new@ucsh.com"}}


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _spy(monkeypatch):
    """Record what would be emailed instead of sending it."""
    sent = []

    async def fake_send(to, subject, html_body, primary_employee_id=None, **kwargs):
        sent.append({"to": to, "subject": subject, "html": html_body, "id": primary_employee_id})

    monkeypatch.setattr(welcome_email, "send_email_with_dashboard", fake_send)
    monkeypatch.setattr(settings, "DASHBOARD_FRONTEND_URL", "https://ooo.example/")
    return sent


def test_it_tells_them_how_to_sign_in_and_links_the_tutorial(monkeypatch):
    sent = _spy(monkeypatch)
    assert run(welcome_email.send_welcome_email(RECORD)) is True
    mail = sent[0]
    assert mail["to"] == ["lee.new@ucsh.com"] and mail["subject"] == "Welcome to UCSH Out of Office"
    assert mail["id"] == "812"                                    # their own dashboard link is appended
    assert "Hi Lee," in mail["html"] and "lee.new@ucsh.com" in mail["html"]
    assert "https://ooo.example/stafftutorial.html" in mail["html"]


def test_nothing_is_sent_without_an_address(monkeypatch):
    sent = _spy(monkeypatch)
    assert run(welcome_email.send_welcome_email({"id": "9", "fields": {"Title": "No Mail"}})) is False
    assert sent == []


def test_add_employee_sends_it(monkeypatch):
    sent = _spy(monkeypatch)
    out = run(dashboard._after_employee_created(dict(RECORD)))
    assert out["welcome_email"] == "sent" and len(sent) == 1


def test_a_failed_send_never_undoes_the_add(monkeypatch):

    async def broken(record):
        raise RuntimeError("email service down")

    monkeypatch.setattr(welcome_email, "send_welcome_email", broken)
    out = run(dashboard._after_employee_created(dict(RECORD)))
    assert out["id"] == "812" and out["welcome_email"] == "failed"
    assert any("welcome email could not be sent" in n for n in out["notices"])
