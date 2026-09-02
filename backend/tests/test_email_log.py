"""Every outbound email leaves a row in email_log, whatever became of it.

Before this table the only trace of an email was a stdout log line, so when
an employee said "I never got it" there was nothing to check. These cover the
four outcomes the client records (sent, partial, failed, skipped), the rule
that the log can never change the result of a send, the person lookup with
its dirty-address normalisation, and the admin endpoint that exposes it.
"""

import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import Base, async_session, engine
from app.graph import email as email_module
from app.models import EmailLog
from app.models.mixins import utcnow
from app.routes import email_log as email_log_route
from app.routes.email_log import router as email_log_router
from app.services import email_log as log_service


# ----- fixtures and helpers -----

async def _reset():
    """Empty the table so each test starts from a known state."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(EmailLog))
        await session.commit()


async def _rows():
    async with async_session() as session:
        result = await session.execute(select(EmailLog).order_by(EmailLog.id))
        return list(result.scalars().all())


def _response(status_code, body):
    """An httpx.Response wired to a request so raise_for_status() works."""
    return httpx.Response(
        status_code, json=body, request=httpx.Request("POST", email_module.SMTP2GO_URL)
    )


def _accepted(email_id="em-1", request_id="req-1"):
    return _response(200, {
        "request_id": request_id,
        "data": {"succeeded": 1, "failed": 0, "failures": [], "email_id": email_id},
    })


class _FakeSmtp2go:
    """Stand-in for the module's httpx client: answers with `outcome` or raises it."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append(json)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def smtp(monkeypatch):
    """Install a fake SMTP2GO that accepts by default; tests override `outcome`."""
    fake = _FakeSmtp2go(_accepted())
    monkeypatch.setattr(email_module, "_http", fake)

    async def _no_wait():
        pass

    # The real limiter sleeps once ten sends land inside a minute.
    monkeypatch.setattr(email_module, "_rate_limit", _no_wait)
    return fake


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(email_log_router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


# ----- recording each outcome -----

def test_accepted_send_is_recorded_with_smtp2go_ids(smtp):
    async def flow():
        await _reset()
        await email_module.send_email(
            to=["  Worker@UCSH.com "],
            subject="Leave Request Received",
            html_body="<p>hi</p>",
            cc=["Boss@ucsh.com"],
            primary_employee_id=412,
        )
        (row,) = await _rows()

        assert row.status == "sent"
        # Stored normalised and comma-wrapped; the payload SMTP2GO saw is untouched.
        assert row.to_addresses == ",worker@ucsh.com,"
        assert row.cc_addresses == ",boss@ucsh.com,"
        assert smtp.calls[0]["to"] == ["  Worker@UCSH.com "]
        assert row.primary_employee_id == "412"
        assert row.subject == "Leave Request Received"
        assert row.smtp2go_email_id == "em-1"
        assert row.smtp2go_request_id == "req-1"
        assert row.http_status == 200
        assert row.error is None

    asyncio.run(flow())


def test_rejected_send_is_recorded_and_still_raises(smtp):
    smtp.outcome = _response(400, {"data": {"error": "bad api key"}})

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError):
            await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        (row,) = await _rows()

        assert row.status == "failed"
        assert row.http_status == 400
        assert "bad api key" in row.error
        assert row.smtp2go_email_id is None

    asyncio.run(flow())


def test_network_failure_is_recorded_and_still_raises(smtp):
    smtp.outcome = httpx.ConnectError("connection refused")

    async def flow():
        await _reset()
        with pytest.raises(httpx.ConnectError):
            await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        (row,) = await _rows()

        assert row.status == "failed"
        # Never answered, so no HTTP status; the exception is the reason.
        assert row.http_status is None
        assert "ConnectError" in row.error
        assert "connection refused" in row.error

    asyncio.run(flow())


def test_partial_acceptance_is_recorded_as_partial(smtp):
    smtp.outcome = _response(200, {
        "request_id": "req-2",
        "data": {
            "succeeded": 1,
            "failed": 1,
            "failures": ["bad@ucsh.com: mailbox unavailable"],
            "email_id": "em-2",
        },
    })

    async def flow():
        await _reset()
        # Partial acceptance does not raise today, and must not start to.
        await email_module.send_email(
            to=["worker@ucsh.com", "bad@ucsh.com"], subject="s", html_body="b"
        )
        (row,) = await _rows()

        assert row.status == "partial"
        assert row.smtp2go_email_id == "em-2"
        assert "bad@ucsh.com" in row.error

    asyncio.run(flow())


def test_blank_recipient_is_recorded_as_skipped_without_calling_smtp2go(smtp):
    async def flow():
        await _reset()
        # fields.get("EmailAddress", "") on a blank directory entry yields "".
        await email_module.send_email(
            to=[""], subject="Leave Request Received", html_body="b", primary_employee_id="412"
        )
        (row,) = await _rows()

        assert smtp.calls == []
        assert row.status == "skipped"
        assert row.to_addresses == ""
        assert row.http_status is None
        # The id is the only handle left for finding this row.
        assert row.primary_employee_id == "412"
        assert "No valid recipient" in row.error

    asyncio.run(flow())


def test_log_write_failure_does_not_break_the_send(smtp, monkeypatch):
    def _db_down():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(log_service, "async_session", _db_down)

    async def flow():
        await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        # The email went out; the bookkeeping failure stayed in the logs.
        assert len(smtp.calls) == 1

    asyncio.run(flow())


def test_dashboard_wrapper_passes_the_employee_id_through(smtp):
    async def flow():
        await _reset()
        await email_module.send_email_with_dashboard(
            to=["mgr@ucsh.com"], subject="s", html_body="b", primary_employee_id="77"
        )
        (row,) = await _rows()

        assert row.primary_employee_id == "77"
        assert row.status == "sent"

    asyncio.run(flow())


# ----- finding a person's emails -----

def test_lookup_matches_by_id_or_address_but_not_substrings():
    async def flow():
        await _reset()
        rec = log_service.record_email
        await rec(status="sent", to=["Ana@ucsh.com "], subject="A", primary_employee_id="1")
        await rec(status="sent", to=["a@ucsh.com"], subject="B")                          # substring trap
        await rec(status="sent", to=["boss@ucsh.com"], cc=["ana@ucsh.com"], subject="C")  # CC match
        await rec(status="failed", to=["other@ucsh.com"], subject="D", primary_employee_id="1")  # id-only match

        by_person = await log_service.find_emails(employee_id=1, address="ANA@ucsh.com")
        assert {r.subject for r in by_person} == {"A", "C", "D"}

        # "a@ucsh.com" is a substring of "ana@ucsh.com"; the comma wrapping keeps them apart.
        by_short_address = await log_service.find_emails(address="a@ucsh.com")
        assert {r.subject for r in by_short_address} == {"B"}

        assert await log_service.find_emails() == []

    asyncio.run(flow())


def test_lookup_honours_the_window_and_returns_newest_first():
    async def flow():
        await _reset()
        await log_service.record_email(status="sent", to=["w@ucsh.com"], subject="old")
        await log_service.record_email(status="sent", to=["w@ucsh.com"], subject="new")
        async with async_session() as session:
            old = (await session.execute(
                select(EmailLog).where(EmailLog.subject == "old")
            )).scalar_one()
            # One day past the default window: the row is kept, just not shown by default.
            old.sent_at = utcnow() - timedelta(days=log_service.DEFAULT_WINDOW_DAYS + 1)
            await session.commit()

        everything = await log_service.find_emails(address="w@ucsh.com")
        assert [r.subject for r in everything] == ["new", "old"]

        recent = await log_service.find_emails(
            address="w@ucsh.com",
            since=utcnow() - timedelta(days=log_service.DEFAULT_WINDOW_DAYS),
        )
        assert [r.subject for r in recent] == ["new"]

    asyncio.run(flow())


# ----- the admin endpoint -----

async def _no_history(**kwargs):
    """Stand-in for the record-based reconstruction, which reads SharePoint."""
    return {"events": [], "notes": [], "latest_dashboard_link_email_at": None}


def _seed_for_route():
    async def flow():
        await _reset()
        await log_service.record_email(
            status="sent", to=["worker@ucsh.com"], subject="Leave Request Received",
            primary_employee_id="412", smtp2go_email_id="em-9",
        )
        await log_service.record_email(status="sent", to=["someone@ucsh.com"], subject="Unrelated")
        await log_service.record_email(
            status="skipped", to=[""], subject="Never sent", primary_employee_id="999",
        )

    asyncio.run(flow())


def test_route_resolves_the_address_from_the_directory(client, monkeypatch):
    async def _employee(item_id):
        return {"id": "412", "fields": {"Title": "Test Worker", "EmailAddress": "Worker@ucsh.com  "}}

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _employee)
    monkeypatch.setattr(email_log_route, "reconstruct_email_history", _no_history)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"employee_id": "412"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "ok"
    assert body["employee_name"] == "Test Worker"
    # The default view is one notification cycle; the response says which window it used.
    assert body["days"] == 30
    assert log_service.DEFAULT_WINDOW_DAYS == 30
    # Trailing whitespace and case in the directory value do not defeat the match.
    assert body["address"] == "worker@ucsh.com"
    assert body["count"] == 1
    (email,) = body["emails"]
    assert email["source"] == "email_log"
    assert email["subject"] == "Leave Request Received"
    assert email["to"] == ["worker@ucsh.com"]
    assert email["smtp2go_email_id"] == "em-9"


def test_route_merges_reconstructed_sends_with_the_send_log_newest_first(client, monkeypatch):
    async def _employee(item_id):
        return {"id": "412", "fields": {"Title": "Test Worker", "EmailAddress": "worker@ucsh.com"}}

    async def _history(**kwargs):
        # The reconstruction receives the resolved identity and the window start.
        assert kwargs["employee_id"] == "412"
        assert kwargs["employee_name"] == "Test Worker"
        assert kwargs["address"] == "worker@ucsh.com"
        assert kwargs["since"] is not None
        old = (utcnow() - timedelta(days=3)).isoformat()
        return {
            "events": [{
                "date": old, "date_precision": "exact", "subject": "Your Dashboard Link",
                "to": ["worker@ucsh.com"], "also_to": None,
                "source": "processing_log dashboard-link-renewal",
                "request_type": None, "request_id": None, "note": None,
            }],
            "notes": ["reminders keep only the latest date"],
            "latest_dashboard_link_email_at": old,
        }

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _employee)
    monkeypatch.setattr(email_log_route, "reconstruct_email_history", _history)
    _seed_for_route()

    body = client.get("/api/dashboard/admin/email-log", params={"employee_id": "412"}).json()
    assert body["count"] == 2
    # Today's send-log row first, the three-day-old reconstructed send second.
    assert [e["source"] for e in body["emails"]] == ["email_log", "processing_log dashboard-link-renewal"]
    assert body["notes"] == ["reminders keep only the latest date"]
    assert body["latest_dashboard_link_email_at"] is not None


def test_route_still_searches_by_id_when_the_directory_has_no_such_employee(client, monkeypatch):
    async def _missing(item_id):
        return None

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _missing)
    monkeypatch.setattr(email_log_route, "reconstruct_email_history", _no_history)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"employee_id": "999"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "not_found"
    assert body["address"] is None
    # The skipped send is exactly the row a support lookup needs to see.
    assert [e["status"] for e in body["emails"]] == ["skipped"]


def test_route_accepts_an_explicit_address_without_a_directory_lookup(client, monkeypatch):
    async def _never(item_id):
        raise AssertionError("directory must not be consulted")

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _never)
    monkeypatch.setattr(email_log_route, "reconstruct_email_history", _no_history)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"address": "WORKER@ucsh.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "skipped"
    assert body["count"] == 1


def test_route_requires_an_id_or_an_address(client):
    assert client.get("/api/dashboard/admin/email-log").status_code == 400
