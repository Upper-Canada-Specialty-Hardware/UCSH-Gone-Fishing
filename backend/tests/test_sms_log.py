"""The Text Log: what send_sms records, delivery callbacks, reply logging, admin.

Developers have no Twilio console, so these two tables (sms_api_log,
sms_reply_log) are the only evidence of what the system texted and what came
back. The tests check: a send writes a row with Twilio's answer scrubbed of the
Account SID; a failed send still raises and still writes a row; a log-write
failure never changes the send; a rehearsal skip is logged; a delivery status
callback updates the row and its history (good signature), is refused on a bad
signature, and is a no-op for an unknown SID; every receive_sms branch writes
the right reply row while the replies sent stay byte-for-byte unchanged; and the
admin lookups and the test-send endpoint.
"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import settings
from app.database import Base, async_session, engine
from app.models import SmsApiLog, SmsReplyLog
from app.models.mixins import utcnow
from app.routes import sms_api_log as sms_log_route
from app.routes import twilio as twilio_route
from app.routes.sms_api_log import router as sms_log_router
from app.routes.twilio import router as twilio_router
from app.services import sms as sms_module
from app.services import sms_api_log as log_service

# A Twilio Account SID shape (AC + 32 hex) to prove it is scrubbed out
# of the stored response body; it is not the configured one on purpose.
_ACCOUNT_SID = "AC" + "0123456789abcdef" * 2   # built at runtime so it never reads as a real SID


# ----- fixtures and helpers -----

async def _reset():
    """Create both tables and empty them so each test starts clean."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(SmsApiLog))
        await session.execute(delete(SmsReplyLog))
        await session.commit()


async def _sends():
    async with async_session() as session:
        return list((await session.execute(select(SmsApiLog).order_by(SmsApiLog.id))).scalars().all())


async def _replies():
    async with async_session() as session:
        return list((await session.execute(select(SmsReplyLog).order_by(SmsReplyLog.id))).scalars().all())


def _twilio_response(status_code, body):
    """An httpx.Response wired to a request so raise_for_status() works."""
    return httpx.Response(
        status_code, json=body,
        request=httpx.Request("POST", "https://api.twilio.com/2010-04-01/Messages.json"),
    )


def _ok_body(sid="SM" + "a" * 32, status="queued"):
    """A Twilio Message resource, carrying the Account SID in its fields/uri."""
    return {
        "sid": sid, "status": status, "account_sid": _ACCOUNT_SID,
        "error_code": None, "error_message": None,
        "uri": f"/2010-04-01/Accounts/{_ACCOUNT_SID}/Messages/{sid}.json",
    }


class _FakeClient:
    """Stand-in for httpx.AsyncClient: answers with `response`, or raises it."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, auth=None, data=None):
        self.calls.append({"url": url, "auth": auth, "data": data})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def twilio(monkeypatch):
    """Install a fake Twilio client that accepts (201) by default."""
    fake = _FakeClient(_twilio_response(201, _ok_body()))
    monkeypatch.setattr(sms_module.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


@pytest.fixture
def client():
    """A bare app mounting the admin text-log router (no admin guard)."""
    app = FastAPI()
    app.include_router(sms_log_router, prefix="/api/dashboard")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def webhook():
    """A bare app mounting the Twilio webhook router."""
    app = FastAPI()
    app.include_router(twilio_router, prefix="/api/twilio")
    return TestClient(app, raise_server_exceptions=False)


# ----- what one send stores (#495) -----

def test_a_send_writes_a_row_with_the_scrubbed_response(twilio):
    async def flow():
        await _reset()
        result = await sms_module.send_sms(
            "(416) 555-0199", "hi there", kind="manager_approval",
            request_kind="leave", request_item_id="77", outbox_id=5, request_id="req-1",
        )
        (row,) = await _sends()

        assert result["sid"].startswith("SM")                      # Twilio's JSON is returned unchanged
        assert row.kind == "manager_approval"
        assert row.request_kind == "leave" and row.request_item_id == "77"
        assert row.outbox_id == 5 and row.request_id == "req-1"
        assert row.to_number == "+14165550199"                     # normalised to E.164 (full, owner ruling)
        assert row.to_digits == "4165550199"                       # the last-ten lookup key
        assert row.from_number == settings.TWILIO_PHONE_NUMBER
        assert row.http_status == 201
        assert row.twilio_sid and row.twilio_status == "queued"
        # The StatusCallback was asked for on the wire (#496).
        assert twilio.calls[0]["data"]["StatusCallback"].endswith("/api/twilio/status")
        # The Account SID is scrubbed out of the stored body; the real one is gone.
        assert _ACCOUNT_SID not in (row.response_body or "")
        assert "AC****" in row.response_body

    asyncio.run(flow())


def test_a_failed_send_still_raises_and_still_writes_a_row(twilio):
    twilio.response = _twilio_response(400, {"code": 21211, "message": "not a valid phone number", "status": 400})

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError):                 # unchanged: a 4xx still raises
            await sms_module.send_sms("+14165550199", "x", kind="result")
        (row,) = await _sends()

        assert row.http_status == 400
        assert row.error_code == "21211"                           # Twilio's numeric code, read off the body
        assert "not a valid phone number" in (row.error_message or "")

    asyncio.run(flow())


def test_an_unparseable_number_raises_and_writes_a_row(twilio):
    async def flow():
        await _reset()
        with pytest.raises(sms_module.InvalidPhoneNumber):         # unchanged: a bad number raises
            await sms_module.send_sms("not-a-number", "x", kind="reminder")
        (row,) = await _sends()

        assert row.http_status is None                             # Twilio was never called
        assert row.to_number == "not-a-number"                     # the bad value kept as evidence
        assert "E.164" in (row.no_response_reason or "")

    asyncio.run(flow())


def test_a_network_error_raises_and_writes_a_row(twilio):
    twilio.response = httpx.ConnectError("connection refused")

    async def flow():
        await _reset()
        with pytest.raises(httpx.ConnectError):
            await sms_module.send_sms("+14165550199", "x")
        (row,) = await _sends()

        assert row.http_status is None
        assert "ConnectError" in (row.no_response_reason or "")

    asyncio.run(flow())


def test_a_log_write_failure_does_not_change_the_send(twilio, monkeypatch):
    def _db_down():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(log_service, "async_session", _db_down)

    async def flow():
        result = await sms_module.send_sms("+14165550199", "x")
        # The text still went out and Twilio's JSON still came back.
        assert len(twilio.calls) == 1
        assert result["sid"].startswith("SM")

    asyncio.run(flow())


def test_a_rehearsal_skip_is_logged(twilio, monkeypatch):
    monkeypatch.setattr("app.services.rehearsal.allow_recipient", lambda to: False)

    async def flow():
        await _reset()
        result = await sms_module.send_sms("+14165550199", "x", kind="reminder")
        assert result == {"skipped": "rehearsal"}                  # unchanged return
        assert twilio.calls == []                                  # Twilio never called
        (row,) = await _sends()
        assert row.http_status is None
        assert "rehearsal" in (row.no_response_reason or "")

    asyncio.run(flow())


# ----- delivery status callbacks (#496) -----

def test_status_callback_updates_the_row_and_its_history(webhook, monkeypatch):
    monkeypatch.setattr(twilio_route, "validate_twilio_signature", lambda *a, **k: True)

    async def seed():
        await _reset()
        await log_service.record_send(
            attempted_at=utcnow(), duration_ms=1, kind="result",
            to_number="+14165550199", from_number="+15555550000", body="b",
            http_status=201, twilio_sid="SMabc", twilio_status="queued",
        )

    asyncio.run(seed())

    r1 = webhook.post("/api/twilio/status", data={"MessageSid": "SMabc", "MessageStatus": "sent"})
    r2 = webhook.post(
        "/api/twilio/status", data={"MessageSid": "SMabc", "MessageStatus": "undelivered", "ErrorCode": "30008"}
    )
    assert r1.status_code == 200 and r2.status_code == 200

    (row,) = asyncio.run(_sends())
    assert row.delivery_status == "undelivered"                    # the latest status wins
    assert row.delivery_error_code == "30008"
    assert row.delivery_updated_at is not None
    import json
    history = json.loads(row.delivery_history)
    assert [h["status"] for h in history] == ["sent", "undelivered"]   # every step kept


def test_status_callback_rejects_a_bad_signature(webhook):
    # No signature header: the real validator fails it, like the inbound webhook.
    r = webhook.post("/api/twilio/status", data={"MessageSid": "SMabc", "MessageStatus": "delivered"})
    assert r.status_code == 403


def test_status_callback_for_an_unknown_sid_is_a_noop_200(webhook, monkeypatch):
    monkeypatch.setattr(twilio_route, "validate_twilio_signature", lambda *a, **k: True)
    asyncio.run(_reset())
    r = webhook.post("/api/twilio/status", data={"MessageSid": "SMnope", "MessageStatus": "delivered"})
    assert r.status_code == 200
    assert asyncio.run(_sends()) == []                             # nothing created, nothing changed


# ----- inbound reply logging (#497) -----

def _fake_send_box(monkeypatch):
    """Replace the webhook's send_sms with a recorder; return the call list."""
    calls = []

    async def fake(to, body, **kwargs):
        calls.append({"to": to, "body": body, "kwargs": kwargs})
        return {"sid": "SMreply"}

    monkeypatch.setattr(twilio_route, "send_sms", fake)
    return calls


def _happy_path(monkeypatch, *, item_fields=None, staff=None, approve_result=None, reject_result=None):
    """Patch the webhook's dependencies for a processed, paused-off run."""
    monkeypatch.setattr(twilio_route, "validate_twilio_signature", lambda *a, **k: True)
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def not_paused():
        return False
    monkeypatch.setattr(twilio_route, "is_paused", not_paused)

    async def get_item(list_id, item_id):
        if item_fields is None:
            raise RuntimeError("not found")
        return {"id": str(item_id), "fields": dict(item_fields)}
    monkeypatch.setattr(twilio_route.request_store, "get_list_item", get_item)

    async def get_staff(list_id):
        return staff or []
    monkeypatch.setattr(twilio_route.staff_store, "get_list_items", get_staff)

    if approve_result is not None:
        async def approve(item_id, sender_id, channel):
            return approve_result
        # setitem restores the real handler after the test, so no leak.
        monkeypatch.setitem(twilio_route.REQUEST_TYPE_CONFIG["leave"], "approve", approve)
    if reject_result is not None:
        async def reject(item_id, sender_id, channel, link_version, reason):
            return reject_result
        monkeypatch.setitem(twilio_route.REQUEST_TYPE_CONFIG["leave"], "reject", reject)


def test_bad_signature_logs_a_reply_and_sends_nothing(webhook, monkeypatch):
    asyncio.run(_reset())
    calls = _fake_send_box(monkeypatch)
    # No signature patch: the real validator fails the empty header.
    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 1", "MessageSid": "SMin"})
    assert r.status_code == 403
    assert calls == []                                             # no reply is sent on a bad signature
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_BAD_SIGNATURE
    assert row.signature_ok is False
    assert row.message_sid == "SMin"


def test_processing_off_logs_a_reply_and_sends_the_same_text(webhook, monkeypatch):
    asyncio.run(_reset())
    monkeypatch.setattr(twilio_route, "validate_twilio_signature", lambda *a, **k: True)
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 1"})
    assert r.status_code == 200
    # The reply text is byte-for-byte what it has always been, and labelled.
    assert calls[0]["body"] == "System is currently in reporting-only mode. Approvals via SMS are disabled."
    assert calls[0]["kwargs"]["kind"] == "reply"
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_PROCESSING_OFF
    # The reply we sent is tied back to this inbound row.
    assert calls[0]["kwargs"]["inbound_reply_id"] == row.id


def test_unrecognized_body_logs_a_reply(webhook, monkeypatch):
    asyncio.run(_reset())
    _happy_path(monkeypatch, item_fields={"Status": "Pending"})   # processing on, not paused
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "gibberish"})
    assert r.status_code == 200
    assert calls[0]["body"] == twilio_route.UNRECOGNIZED_SMS_REPLY
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_UNRECOGNIZED


def test_not_found_logs_a_reply(webhook, monkeypatch):
    asyncio.run(_reset())
    _happy_path(monkeypatch, item_fields=None)                    # get_list_item raises
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 123"})
    assert r.status_code == 200
    assert "does not exist" in calls[0]["body"]
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_NOT_FOUND
    assert row.decision == "Approve" and row.item_id == "123" and row.request_type == "leave"


def test_not_registered_logs_a_reply(webhook, monkeypatch):
    asyncio.run(_reset())
    _happy_path(monkeypatch, item_fields={"Status": "Pending"}, staff=[])   # no staff match
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 123"})
    assert r.status_code == 200
    assert "not registered" in calls[0]["body"]
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_NOT_REGISTERED


def test_no_access_logs_a_reply(webhook, monkeypatch):
    asyncio.run(_reset())
    # A staff member matches the number but is not an admin and not the manager.
    staff = [{"id": "9", "fields": {"Title": "Someone Else", "CellNumber": "+14165550199"}}]
    _happy_path(monkeypatch, item_fields={"Status": "Pending", "ManagerLookupId": "5"}, staff=staff)
    calls = _fake_send_box(monkeypatch)

    async def no_submitter(fields, column):
        return None
    monkeypatch.setattr("app.services.request_submitter.resolve_request_submitter", no_submitter)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 123"})
    assert r.status_code == 200
    assert "do not have access" in calls[0]["body"]
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_NO_ACCESS
    assert row.sender_staff_id == "9"                              # the matched sender is recorded


def test_approved_logs_a_reply_and_sends_the_same_text(webhook, monkeypatch):
    asyncio.run(_reset())
    # Sender is an admin (Jay Puzon), so authorization passes with no manager lookup.
    staff = [{"id": "3", "fields": {"Title": "Jay Puzon", "CellNumber": "+14165550199", "EmailAddress": "jay@ucsh.com"}}]
    _happy_path(monkeypatch, item_fields={"Status": "Pending"}, staff=staff, approve_result={})
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 123"})
    assert r.status_code == 200
    assert calls[0]["body"].startswith("Response has been received. An email will be sent to (jay@ucsh.com)")
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_APPROVED
    assert row.decision == "Approve" and row.item_id == "123"


def test_rejected_logs_a_reply_and_sends_the_same_text(webhook, monkeypatch):
    asyncio.run(_reset())
    staff = [{"id": "3", "fields": {"Title": "Jay Puzon", "CellNumber": "+14165550199", "EmailAddress": "jay@ucsh.com"}}]
    _happy_path(monkeypatch, item_fields={"Status": "Pending"}, staff=staff, reject_result={})
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Reject 123 covered already"})
    assert r.status_code == 200
    assert calls[0]["body"] == "Response has been received. Cancelling request #123."
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_REJECTED
    assert row.decision == "Reject" and row.reason == "covered already"


def test_refused_logs_the_handlers_error(webhook, monkeypatch):
    asyncio.run(_reset())
    staff = [{"id": "3", "fields": {"Title": "Jay Puzon", "CellNumber": "+14165550199", "EmailAddress": "jay@ucsh.com"}}]
    _happy_path(
        monkeypatch, item_fields={"Status": "Pending"}, staff=staff,
        approve_result={"error": "clashes with an approved absence"},
    )
    calls = _fake_send_box(monkeypatch)

    r = webhook.post("/api/twilio/sms", data={"From": "+14165550199", "Body": "LR Approve 123"})
    assert r.status_code == 200
    assert "was not approved. clashes with an approved absence" in calls[0]["body"]
    (row,) = asyncio.run(_replies())
    assert row.outcome == log_service.OUTCOME_REFUSED
    assert row.error_text == "clashes with an approved absence"


# ----- the admin lookups and the test send (#498) -----

async def _seed_messages():
    """A send to a cell, an inbound reply from it, and one about a request."""
    await _reset()
    now = utcnow()
    await log_service.record_send(
        attempted_at=now, duration_ms=1, kind="manager_approval",
        to_number="+14165550199", from_number="+15555550000", body="please decide #7",
        request_kind="leave", request_item_id="7", http_status=201, twilio_sid="SM7",
    )
    await log_service.record_reply(
        message_sid="SMin7", from_number="(416) 555-0199", to_number="+15555550000",
        body="LR Approve 7", signature_ok=True,
    )
    await log_service.update_reply(
        (await _replies())[-1].id, outcome=log_service.OUTCOME_APPROVED,
        decision="Approve", request_type="leave", item_id="7",
    )


def test_admin_lookup_by_employee_matches_on_the_last_ten_digits(client, monkeypatch):
    async def _employee(item_id):
        return {"id": "42", "fields": {"Title": "Pat Worker", "CellNumber": "416-555-0199"}}

    monkeypatch.setattr(sms_log_route, "get_employee_by_id", _employee)
    asyncio.run(_seed_messages())

    r = client.get("/api/dashboard/admin/sms-log", params={"employee_id": "42"})
    assert r.status_code == 200
    body = r.json()
    assert body["directory_lookup"] == "ok"
    assert body["employee_name"] == "Pat Worker"
    # Both the send (to this cell) and the reply (from this cell) are found.
    assert len(body["sends"]) == 1 and len(body["replies"]) == 1
    assert body["sends"][0]["to_number"] == "+14165550199"
    assert body["replies"][0]["decision"] == "Approve"


def test_admin_lookup_by_request(client):
    asyncio.run(_seed_messages())
    r = client.get("/api/dashboard/admin/sms-log", params={"request_kind": "leave", "item_id": "7"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["sends"]) == 1 and len(body["replies"]) == 1
    assert body["sends"][0]["request_item_id"] == "7"


def test_admin_lookup_needs_a_scope(client):
    assert client.get("/api/dashboard/admin/sms-log").status_code == 400


def test_admin_recent_lists_sends_and_replies(client):
    asyncio.run(_seed_messages())
    r = client.get("/api/dashboard/admin/sms-log/recent", params={"hours": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert len(body["sends"]) == 1 and len(body["replies"]) == 1


def test_admin_test_send_goes_to_the_cell_and_returns_the_row(client, monkeypatch):
    async def _employee(item_id):
        return {"id": "42", "fields": {"Title": "Pat Worker", "CellNumber": "+14165550199"}}

    monkeypatch.setattr(sms_log_route, "get_employee_by_id", _employee)

    sent = {}

    async def fake_send(to, body, **kwargs):
        sent["to"] = to
        sent["kind"] = kwargs.get("kind")
        # Mimic send_sms writing its own row, so the endpoint can find it.
        await log_service.record_send(
            attempted_at=utcnow(), duration_ms=1, kind=kwargs.get("kind", "other"),
            to_number=to, from_number="+15555550000", body=body, http_status=201, twilio_sid="SMtest",
        )
        return {"sid": "SMtest"}

    monkeypatch.setattr(sms_log_route, "send_sms", fake_send)
    asyncio.run(_reset())

    r = client.post("/api/dashboard/admin/sms-log/test", json={"employee_id": "42"})
    assert r.status_code == 200
    body = r.json()
    assert sent["to"] == "+14165550199" and sent["kind"] == "test"
    assert body["send"] is not None
    assert body["send"]["body"].startswith("This is a test text from the UCSH Out of Office admin dashboard")


def test_admin_test_send_refuses_when_there_is_no_number(client, monkeypatch):
    async def _blank(item_id):
        return {"id": "5", "fields": {"Title": "No Cell", "CellNumber": ""}}

    monkeypatch.setattr(sms_log_route, "get_employee_by_id", _blank)
    r = client.post("/api/dashboard/admin/sms-log/test", json={"employee_id": "5"})
    assert r.status_code == 400
    assert "no cell number" in r.json()["detail"].lower()
    assert client.post("/api/dashboard/admin/sms-log/test", json={}).status_code == 400
