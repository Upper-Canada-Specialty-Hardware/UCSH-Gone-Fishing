"""Every SMTP2GO call leaves a row holding the request and the answer verbatim.

The question this log exists to settle: did the backend ask SMTP2GO to send
the email, and what did SMTP2GO say back. So the tests check what is stored
(the redacted request, the raw response body, the derived outcome, the
recipient rows), that storing it never changes what a send does, the person
lookup, and the admin endpoint that exposes it.
"""

import asyncio
import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database import Base, async_session, engine
from app.graph import email as email_module
from app.models import EmailApiLog, EmailApiLogRecipient
from app.models.mixins import utcnow
from app.routes import email_api_log as email_log_route
from app.routes.email_api_log import router as email_log_router
from app.services import email_api_log as log_service


# ----- fixtures and helpers -----

async def _reset():
    """Empty both tables so each test starts from a known state."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(EmailApiLogRecipient))
        await session.execute(delete(EmailApiLog))
        await session.commit()


async def _rows():
    async with async_session() as session:
        result = await session.execute(select(EmailApiLog).order_by(EmailApiLog.id))
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


async def _seed(*, to, subject, cc=None, outcome=None, attempted_at=None, email_id="em-x"):
    """Write one exchange directly, the way an accepted send would."""
    body = json.dumps({
        "request_id": "req-x",
        "data": {"succeeded": len(to), "failed": 0, "failures": [], "email_id": email_id},
    })
    summary = log_service.classify_response(200, body)
    if outcome:
        summary.outcome = outcome
    payload = {"api_key": "k", "sender": "hr@x.com", "to": to, "subject": subject, "html_body": "b"}
    if cc:
        payload["cc"] = cc
    await log_service.record_exchange(
        summary,
        request_url=email_module.SMTP2GO_URL,
        payload=payload,
        attempted_at=attempted_at or utcnow(),
        duration_ms=12,
    )


# ----- what one send stores -----

def test_accepted_send_stores_the_request_and_the_answer_verbatim(smtp):
    async def flow():
        await _reset()
        summary = await email_module.send_email(
            to=["  Worker@UCSH.com "],
            subject="Leave Request Received",
            html_body="<p>hi</p>",
            cc=["Boss@ucsh.com"],
            importance="High",
        )
        (row,) = await _rows()

        # The answer, exactly as SMTP2GO gave it, plus what was read off it.
        assert row.http_status == 200
        assert json.loads(row.response_body) == json.loads(smtp.outcome.text)
        assert row.outcome == "accepted"
        assert row.succeeded_count == 1
        assert row.failed_count == 0
        assert row.smtp2go_email_id == "em-1"
        assert row.smtp2go_request_id == "req-1"
        assert row.no_response_reason is None
        assert row.duration_ms is not None
        assert row.request_url == email_module.SMTP2GO_URL

        # The request, minus the two things that must never be stored.
        request = json.loads(row.request_json)
        assert "api_key" not in request
        assert "html_body" not in request
        assert request["html_body_bytes"] == len(b"<p>hi</p>")
        assert request["html_body_sha256"] == hashlib.sha256(b"<p>hi</p>").hexdigest()
        # Recipients as sent (untouched), so the row shows what SMTP2GO saw.
        assert request["to"] == ["  Worker@UCSH.com "]
        assert request["cc"] == ["Boss@ucsh.com"]
        assert request["custom_headers"][1] == {"header": "Importance", "value": "High"}
        assert smtp.calls[0]["to"] == ["  Worker@UCSH.com "]
        assert row.sender == smtp.calls[0]["sender"]
        assert row.subject == "Leave Request Received"

        # Recipient rows are the normalised lookup key.
        assert sorted((r.field, r.address) for r in row.recipients) == [
            ("cc", "boss@ucsh.com"),
            ("to", "worker@ucsh.com"),
        ]

        # The caller gets the same reading back.
        assert summary.outcome == "accepted"
        assert summary.email_id == "em-1"

    asyncio.run(flow())


def test_http_error_is_stored_and_still_raises(smtp):
    smtp.outcome = _response(400, {"data": {"error": "bad api key"}})

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError):
            await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        (row,) = await _rows()

        assert row.outcome == "http_error"
        assert row.http_status == 400
        assert "bad api key" in row.response_body          # SMTP2GO's own words, kept
        assert row.smtp2go_email_id is None

    asyncio.run(flow())


def test_network_failure_is_stored_and_still_raises(smtp):
    smtp.outcome = httpx.ConnectError("connection refused")

    async def flow():
        await _reset()
        with pytest.raises(httpx.ConnectError):
            await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        (row,) = await _rows()

        assert row.outcome == "no_response"
        # Never answered, so no status and no body; the exception is the reason.
        assert row.http_status is None
        assert row.response_body is None
        assert "ConnectError" in row.no_response_reason
        assert "connection refused" in row.no_response_reason

    asyncio.run(flow())


def test_partial_acceptance_is_stored_and_does_not_raise(smtp):
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
        # Existing behaviour: a 200 with per-recipient failures does not raise.
        summary = await email_module.send_email(
            to=["worker@ucsh.com", "bad@ucsh.com"], subject="s", html_body="b"
        )
        (row,) = await _rows()

        assert row.outcome == "partially_accepted"
        assert (row.succeeded_count, row.failed_count) == (1, 1)
        assert row.smtp2go_email_id == "em-2"
        assert "mailbox unavailable" in row.response_body
        assert summary.failed == 1

    asyncio.run(flow())


def test_rejection_on_http_200_is_stored_as_rejected_and_does_not_raise(smtp):
    smtp.outcome = _response(200, {
        "request_id": "req-3",
        "data": {"succeeded": 0, "failed": 1, "failures": ["worker@ucsh.com: suppressed"]},
    })

    async def flow():
        await _reset()
        summary = await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        (row,) = await _rows()

        # This is the case the log exists to expose: HTTP 200, nothing queued.
        assert row.http_status == 200
        assert row.outcome == "rejected"
        assert row.smtp2go_email_id is None
        assert summary.outcome == "rejected"

    asyncio.run(flow())


def test_blank_recipient_is_stored_as_not_attempted_without_calling_smtp2go(smtp):
    async def flow():
        await _reset()
        # fields.get("EmailAddress", "") on a blank directory entry yields "".
        summary = await email_module.send_email(
            to=[""], subject="Leave Request Received", html_body="b"
        )
        (row,) = await _rows()

        assert smtp.calls == []
        assert row.outcome == "not_attempted"
        assert row.http_status is None
        assert row.duration_ms is None
        assert "No valid recipient" in row.no_response_reason
        # The request shows the blank the code was given; no recipient row exists.
        assert json.loads(row.request_json)["to"] == [""]
        assert row.recipients == []
        assert summary.outcome == "not_attempted"

    asyncio.run(flow())


def test_log_write_failure_does_not_break_the_send(smtp, monkeypatch):
    def _db_down():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(log_service, "async_session", _db_down)

    async def flow():
        summary = await email_module.send_email(to=["worker@ucsh.com"], subject="s", html_body="b")
        # The email went out and the caller still gets SMTP2GO's reading.
        assert len(smtp.calls) == 1
        assert summary.outcome == "accepted"

    asyncio.run(flow())


def test_dashboard_wrapper_still_sends_and_logs(smtp):
    async def flow():
        await _reset()
        await email_module.send_email_with_dashboard(
            to=["mgr@ucsh.com"], subject="s", html_body="b", primary_employee_id="77"
        )
        (row,) = await _rows()
        assert row.outcome == "accepted"
        assert [r.address for r in row.recipients] == ["mgr@ucsh.com"]

    asyncio.run(flow())


# ----- reading the answer (pure) -----

def test_classify_reads_the_documented_shapes():
    accepted = log_service.classify_response(
        200, json.dumps({"request_id": "r", "data": {"succeeded": 2, "failed": 0, "email_id": "e"}})
    )
    assert (accepted.outcome, accepted.succeeded, accepted.failed) == ("accepted", 2, 0)
    assert (accepted.email_id, accepted.request_id) == ("e", "r")

    partial = log_service.classify_response(
        200, json.dumps({"data": {"succeeded": 1, "failed": 1}})
    )
    assert partial.outcome == "partially_accepted"

    rejected = log_service.classify_response(200, json.dumps({"data": {"succeeded": 0, "failed": 2}}))
    assert rejected.outcome == "rejected"

    # A 4xx is an HTTP error even when its body parses.
    http_error = log_service.classify_response(401, json.dumps({"data": {"error": "x"}}))
    assert http_error.outcome == "http_error"
    assert http_error.response_body is not None

    # A 2xx that is not the documented JSON is kept, and flagged as unreadable.
    unreadable = log_service.classify_response(200, "<html>maintenance</html>")
    assert unreadable.outcome == "unreadable_response"
    assert unreadable.response_body == "<html>maintenance</html>"

    none = log_service.classify_response(None, None, "ReadTimeout: timed out")
    assert (none.outcome, none.no_response_reason) == ("no_response", "ReadTimeout: timed out")


def test_redact_request_drops_the_key_and_the_body_only():
    safe = log_service.redact_request({
        "api_key": "secret", "sender": "hr@x.com", "to": ["a@x.com"],
        "subject": "s", "html_body": "<p>x</p>", "cc": ["c@x.com"],
    })
    assert "api_key" not in safe
    assert "html_body" not in safe
    assert safe["html_body_bytes"] == 8
    assert safe["to"] == ["a@x.com"] and safe["cc"] == ["c@x.com"]
    assert safe["sender"] == "hr@x.com" and safe["subject"] == "s"


# ----- finding a person's exchanges -----

def test_lookup_matches_to_and_cc_exactly_and_never_duplicates():
    async def flow():
        await _reset()
        await _seed(to=["Ana@ucsh.com "], subject="A")                       # dirty To
        await _seed(to=["a@ucsh.com"], subject="B")                          # substring trap
        await _seed(to=["boss@ucsh.com"], cc=["ana@ucsh.com"], subject="C")  # CC match
        await _seed(to=["ana@ucsh.com"], cc=["ANA@ucsh.com"], subject="D")   # in both fields

        found = await log_service.find_exchanges(address="ANA@ucsh.com")
        assert [r.subject for r in found] == ["D", "C", "A"]                 # newest first, D once

        # "a@ucsh.com" is not a substring match on "ana@ucsh.com".
        short = await log_service.find_exchanges(address="a@ucsh.com")
        assert [r.subject for r in short] == ["B"]

        assert await log_service.find_exchanges(address="") == []
        assert await log_service.find_exchanges(address=None) == []

    asyncio.run(flow())


def test_lookup_honours_the_window_and_reports_coverage():
    async def flow():
        await _reset()
        assert await log_service.log_coverage_start() is None                # empty table

        old_time = utcnow() - timedelta(days=log_service.DEFAULT_WINDOW_DAYS + 1)
        await _seed(to=["w@ucsh.com"], subject="old", attempted_at=old_time)
        await _seed(to=["w@ucsh.com"], subject="new")

        everything = await log_service.find_exchanges(address="w@ucsh.com")
        assert [r.subject for r in everything] == ["new", "old"]

        recent = await log_service.find_exchanges(
            address="w@ucsh.com",
            since=utcnow() - timedelta(days=log_service.DEFAULT_WINDOW_DAYS),
        )
        assert [r.subject for r in recent] == ["new"]                        # old kept, not shown

        coverage = await log_service.log_coverage_start()
        assert abs((coverage - old_time).total_seconds()) < 1

    asyncio.run(flow())


# ----- the admin endpoint -----

def _seed_for_route():
    async def flow():
        await _reset()
        await _seed(to=["worker@ucsh.com"], subject="Leave Request Received", email_id="em-9")
        await _seed(to=["someone@ucsh.com"], subject="Unrelated")

    asyncio.run(flow())


def test_route_resolves_the_address_from_the_directory(client, monkeypatch):
    async def _employee(item_id):
        return {"id": "412", "fields": {"Title": "Test Worker", "EmailAddress": "Worker@ucsh.com  "}}

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _employee)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"employee_id": "412"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "ok"
    assert body["employee_name"] == "Test Worker"
    assert body["days"] == 30 == log_service.DEFAULT_WINDOW_DAYS
    # Trailing whitespace and case in the directory value do not defeat the match.
    assert body["address"] == "worker@ucsh.com"
    assert body["log_since"] is not None
    assert body["count"] == 1
    (email,) = body["emails"]
    assert email["subject"] == "Leave Request Received"
    assert email["to"] == ["worker@ucsh.com"]
    assert email["cc"] == []
    assert email["outcome"] == "accepted"
    assert email["http_status"] == 200
    assert email["smtp2go_email_id"] == "em-9"
    # The evidence travels with the row: request as an object, response verbatim.
    assert email["request"]["to"] == ["worker@ucsh.com"]
    assert "api_key" not in email["request"]
    assert json.loads(email["response_body"])["data"]["email_id"] == "em-9"


def test_route_says_when_the_directory_has_no_such_employee(client, monkeypatch):
    async def _missing(item_id):
        return None

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _missing)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"employee_id": "999"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "not_found"
    assert body["address"] is None
    assert body["emails"] == []


def test_route_says_when_the_employee_has_no_address(client, monkeypatch):
    async def _blank(item_id):
        return {"id": "5", "fields": {"Title": "No Email", "EmailAddress": "   "}}

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _blank)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"employee_id": "5"})
    body = r.json()
    # Nothing to search for, and that is the finding: the code could not have emailed them.
    assert body["directory_lookup"] == "no_address"
    assert body["employee_name"] == "No Email"
    assert body["emails"] == []


def test_route_accepts_an_explicit_address_without_a_directory_lookup(client, monkeypatch):
    async def _never(item_id):
        raise AssertionError("directory must not be consulted")

    monkeypatch.setattr(email_log_route, "get_employee_by_id", _never)
    _seed_for_route()

    r = client.get("/api/dashboard/admin/email-log", params={"address": "WORKER@ucsh.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "skipped"
    assert body["count"] == 1


def test_route_requires_an_id_or_an_address(client):
    assert client.get("/api/dashboard/admin/email-log").status_code == 400
