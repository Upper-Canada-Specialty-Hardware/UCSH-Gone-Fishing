"""Routing one send across SMTP2GO, the UCSH mailer and Clerk.

These cover the new per-recipient routing in ``app/graph/email.py`` and the two
new clients. The guarantees under test: each recipient reaches the service its
domain designates (UCSH -> mailer, else -> Clerk, anything switched off ->
SMTP2GO); each client builds the right request with its secret in a header; the
mailer and Clerk retry only what they are allowed to, with the same idempotency
key, and leave exactly one redacted log row per logical call; a misconfigured
but enabled service falls back to SMTP2GO; one failing group does not stop the
others and the first failure is raised; and with both new services off the
behaviour and logging are unchanged.
"""

import asyncio
import json
import re

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import Base, async_session, engine
from app.graph import clerk_email, email as email_module, mailer_client
from app.models import EmailApiLog, EmailApiLogRecipient
from app.services import email_api_log as log_service


# ----- fixtures and helpers -----

async def _reset():
    """Empty both log tables so each test starts from a known state."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(EmailApiLogRecipient))
        await session.execute(delete(EmailApiLog))
        await session.commit()


async def _rows():
    """Every stored log row, oldest first."""
    async with async_session() as session:
        result = await session.execute(select(EmailApiLog).order_by(EmailApiLog.id))
        return list(result.scalars().all())


def _resp(status, body=None, headers=None, url="https://svc.example/endpoint"):
    """An httpx.Response wired to a request so raise_for_status() works."""
    return httpx.Response(
        status,
        json=body if body is not None else {},
        headers=headers or {},
        request=httpx.Request("POST", url),
    )


class _FakeHttp:
    """Stand-in AsyncClient: records each post, returns scripted responses.

    A single response (or exception) is returned for every call; a list is
    walked in order, the last entry repeating once exhausted, which is how the
    retry tests script a sequence like 429, 429, 200.
    """

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = []
        self._i = 0

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        item = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def no_sleep(monkeypatch):
    """Make both clients' retry backoff instant."""
    async def _instant(_seconds):
        pass

    monkeypatch.setattr(mailer_client, "_sleep", _instant)
    monkeypatch.setattr(clerk_email, "_sleep", _instant)


def _mailer_accepted(status="sent", recipients=1, message_ids=("m1",)):
    return _resp(200, {
        "status": status,
        "recipients": recipients,
        "messages": [{"messageId": mid, "recipients": ["x@ucsh.com"]} for mid in message_ids],
    })


def _clerk_accepted(email_id="eml_1", suppression_reason=None):
    return _resp(200, {
        "id": email_id,
        "status": "queued",
        "to_email_address": "jane@gmail.com",
        "delivered_by_clerk": True,
        "suppression_reason": suppression_reason,
    })


def _enable_mailer(monkeypatch):
    monkeypatch.setattr(settings, "MAILER_ENABLED", True)
    monkeypatch.setattr(settings, "MAILER_URL", "https://mailer.internal")
    monkeypatch.setattr(settings, "MAILER_KEY", "mk_secret")
    monkeypatch.setattr(settings, "MAILER_FROM", "ooo@ucsh.com")


def _enable_clerk(monkeypatch):
    monkeypatch.setattr(settings, "CLERK_EMAIL_ENABLED", True)
    monkeypatch.setattr(settings, "CLERK_SECRET_KEY", "sk_live_secret")
    monkeypatch.setattr(settings, "CLERK_FROM_EMAIL", "noreply@verified.com")


def _install_smtp2go(monkeypatch, response):
    """Point the SMTP2GO path at a fake and skip its real rate limiter."""
    fake = _FakeHttp(response)
    monkeypatch.setattr(email_module, "_http", fake)

    async def _no_wait():
        pass

    monkeypatch.setattr(email_module, "_rate_limit", _no_wait)
    return fake


# ----- routing by domain for each switch combination -----

def test_routing_sends_everything_to_smtp2go_when_both_off():
    smtp, mailer, clerk = email_module._route_recipients(
        ["staff@ucsh.com", "temp@ucaccess.com"], ["out@gmail.com"]
    )
    assert smtp == {"to": ["staff@ucsh.com", "temp@ucaccess.com"], "cc": ["out@gmail.com"]}
    assert mailer == {"to": [], "cc": []}
    assert clerk == []


def test_routing_internal_to_mailer_external_to_smtp2go_when_only_mailer_on(monkeypatch):
    _enable_mailer(monkeypatch)
    smtp, mailer, clerk = email_module._route_recipients(
        ["staff@ucsh.com"], ["out@gmail.com"]
    )
    assert mailer == {"to": ["staff@ucsh.com"], "cc": []}
    assert smtp == {"to": [], "cc": ["out@gmail.com"]}  # external stays on smtp2go, clerk off
    assert clerk == []


def test_routing_external_to_clerk_internal_to_smtp2go_when_only_clerk_on(monkeypatch):
    _enable_clerk(monkeypatch)
    smtp, mailer, clerk = email_module._route_recipients(
        ["staff@ucsh.com"], ["out@gmail.com"]
    )
    assert clerk == [("out@gmail.com", "cc")]
    assert smtp == {"to": ["staff@ucsh.com"], "cc": []}  # internal stays on smtp2go, mailer off
    assert mailer == {"to": [], "cc": []}


def test_routing_splits_internal_and_external_when_both_on(monkeypatch):
    _enable_mailer(monkeypatch)
    _enable_clerk(monkeypatch)
    smtp, mailer, clerk = email_module._route_recipients(
        ["staff@ucsh.com", "out@gmail.com"], ["boss@ucsh.com", "vendor@example.org"]
    )
    assert mailer == {"to": ["staff@ucsh.com"], "cc": ["boss@ucsh.com"]}
    assert sorted(clerk) == [("out@gmail.com", "to"), ("vendor@example.org", "cc")]
    assert smtp == {"to": [], "cc": []}  # nothing falls through


# ----- the UCSH mailer payload, headers and classifier -----

def test_mailer_payload_shape_and_cc_rule(monkeypatch):
    _enable_mailer(monkeypatch)
    monkeypatch.setattr(settings, "MAILER_FROM_NAME", "UCSH Out of Office")

    small = mailer_client.build_payload(["a@ucsh.com"], ["b@ucsh.com"], "Subj", "<p>hi</p>")
    assert small == {
        "to": ["a@ucsh.com"],
        "cc": ["b@ucsh.com"],
        "subject": "Subj",
        "html": "<p>hi</p>",
        "from": "ooo@ucsh.com",
        "fromName": "UCSH Out of Office",
    }

    # Above 50 recipients the mailer splits, so cc is folded into to.
    big_to = [f"u{i}@ucsh.com" for i in range(50)]
    big = mailer_client.build_payload(big_to, ["b@ucsh.com"], "Subj", "<p>hi</p>")
    assert "cc" not in big
    assert big["to"] == big_to + ["b@ucsh.com"]


def test_mailer_send_posts_bearer_and_idempotency_header(monkeypatch):
    _enable_mailer(monkeypatch)
    fake = _FakeHttp(_mailer_accepted(recipients=1))
    monkeypatch.setattr(mailer_client, "_http", fake)

    async def flow():
        await _reset()
        summary = await mailer_client.send(["a@ucsh.com"], [], "Subj", "<p>hi</p>", "key-123")
        (call,) = fake.calls
        assert call["url"] == "https://mailer.internal/v1/send"
        assert call["headers"]["Authorization"] == "Bearer mk_secret"
        assert call["headers"]["Idempotency-Key"] == "key-123"
        assert summary.outcome == "accepted"
        assert summary.email_id == "m1"
        # One row, redacted: no key anywhere, html summarised not stored.
        (row,) = await _rows()
        assert row.request_url == "https://mailer.internal/v1/send"
        req = json.loads(row.request_json)
        assert "mk_secret" not in row.request_json and "Authorization" not in req
        assert "html" not in req and req["html_bytes"] == len(b"<p>hi</p>")
        assert log_service.service_for_url(row.request_url) == "ucsh_mailer"

    asyncio.run(flow())


def test_mailer_classifier_reads_sent_and_logged():
    sent = mailer_client.classify_response(200, json.dumps(
        {"status": "sent", "recipients": 2, "messages": [
            {"messageId": "m1"}, {"messageId": "m2"}]}
    ))
    assert (sent.outcome, sent.succeeded, sent.failed) == ("accepted", 2, 0)
    assert sent.email_id == "m1,m2"  # every split message id is kept

    logged = mailer_client.classify_response(200, json.dumps(
        {"status": "logged", "recipients": 1, "messages": []}))
    assert logged.outcome == "accepted"  # mailer running with sending off still accepted the call

    err = mailer_client.classify_response(502, json.dumps(
        {"error": "hve_unavailable", "message": "down"}))
    assert err.outcome == "http_error"
    assert "hve_unavailable" in err.response_body


# ----- the UCSH mailer retry rules -----

@pytest.mark.parametrize("script, expected_calls", [
    ([_resp(429, {"error": "rate_limited"}, {"Retry-After": "0"}), _mailer_accepted()], 2),
    ([_resp(503, {"error": "hve_unavailable"}), _resp(503, {"error": "hve_unavailable"}),
      _mailer_accepted()], 3),
    ([_resp(502, {"error": "hve_token_failed"}), _mailer_accepted()], 2),
])
def test_mailer_retries_only_the_allowed_failures(monkeypatch, no_sleep, script, expected_calls):
    _enable_mailer(monkeypatch)
    fake = _FakeHttp(script)
    monkeypatch.setattr(mailer_client, "_http", fake)

    async def flow():
        await _reset()
        summary = await mailer_client.send(["a@ucsh.com"], [], "S", "<p>b</p>", "same-key")
        assert len(fake.calls) == expected_calls
        # Same idempotency key on every attempt.
        assert {c["headers"]["Idempotency-Key"] for c in fake.calls} == {"same-key"}
        assert summary.outcome == "accepted"
        assert len(await _rows()) == 1  # one row holding the final answer, not one per attempt

    asyncio.run(flow())


def test_mailer_caps_at_three_attempts(monkeypatch, no_sleep):
    _enable_mailer(monkeypatch)
    fake = _FakeHttp([_resp(429, {"error": "rate_limited"}, {"Retry-After": "0"})])
    monkeypatch.setattr(mailer_client, "_http", fake)

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError):
            await mailer_client.send(["a@ucsh.com"], [], "S", "<p>b</p>", "k")
        assert len(fake.calls) == 3  # first attempt plus two retries, then give up

    asyncio.run(flow())


@pytest.mark.parametrize("status, code", [
    (400, "invalid_request"), (401, "unauthorized"), (403, "sender_not_allowed"),
    (422, "idempotency_key_reused"),
])
def test_mailer_does_not_retry_hard_errors(monkeypatch, no_sleep, status, code):
    _enable_mailer(monkeypatch)
    fake = _FakeHttp(_resp(status, {"error": code, "message": "no"}))
    monkeypatch.setattr(mailer_client, "_http", fake)

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError):
            await mailer_client.send(["a@ucsh.com"], [], "S", "<p>b</p>", "k")
        assert len(fake.calls) == 1  # one attempt, no retry
        (row,) = await _rows()
        assert row.outcome == "http_error" and row.http_status == status

    asyncio.run(flow())


# ----- Clerk one call per recipient -----

def test_clerk_send_one_call_per_recipient_with_right_json_and_key(monkeypatch):
    _enable_clerk(monkeypatch)
    monkeypatch.setattr(settings, "CLERK_REPLY_TO", "reply@verified.com")
    fake = _FakeHttp(_clerk_accepted())
    monkeypatch.setattr(clerk_email, "_http", fake)

    async def flow():
        await _reset()
        summary = await clerk_email.send("jane@gmail.com", "to", "Subj", "<p>hi</p>", "base_abc")
        (call,) = fake.calls
        assert call["url"] == "https://api.clerk.com/v1/email"
        assert call["json"] == {
            "to": {"address": "jane@gmail.com"},
            "from": {"address": "noreply@verified.com"},
            "subject": "Subj",
            "html": "<p>hi</p>",
            "reply_to": {"address": "reply@verified.com"},
        }
        assert call["headers"]["Authorization"] == "Bearer sk_live_secret"
        assert call["headers"]["Idempotency-Key"] == "base_abc"
        assert summary.outcome == "accepted" and summary.email_id == "eml_1"
        (row,) = await _rows()
        assert "sk_live_secret" not in row.request_json
        assert [r.address for r in row.recipients] == ["jane@gmail.com"]
        assert log_service.service_for_url(row.request_url) == "clerk"

    asyncio.run(flow())


def test_clerk_key_is_within_the_allowed_charset():
    key = email_module._clerk_key("abc123", "Jane.Doe+tag@gmail.com")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,255}", key)
    # Same base + same address is stable (so a retry reuses the key).
    assert key == email_module._clerk_key("abc123", "jane.doe+tag@GMAIL.com")


def test_clerk_suppression_is_recorded_as_rejected(monkeypatch):
    _enable_clerk(monkeypatch)
    fake = _FakeHttp(_clerk_accepted(suppression_reason="hard_bounce"))
    monkeypatch.setattr(clerk_email, "_http", fake)

    async def flow():
        await _reset()
        summary = await clerk_email.send("jane@gmail.com", "to", "S", "<p>b</p>", "k")
        assert summary.outcome == "rejected" and summary.failed == 1

    asyncio.run(flow())


# ----- end to end through send_email -----

def test_send_email_fans_out_to_mailer_and_clerk(monkeypatch):
    _enable_mailer(monkeypatch)
    _enable_clerk(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_mailer_accepted(recipients=1))
    c = _FakeHttp(_clerk_accepted())
    monkeypatch.setattr(mailer_client, "_http", m)
    monkeypatch.setattr(clerk_email, "_http", c)

    async def flow():
        await _reset()
        summary = await email_module.send_email(
            to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>hi</p>"
        )
        assert smtp.calls == []            # no recipient fell through to smtp2go
        assert len(m.calls) == 1           # one mailer call for the internal group
        assert len(c.calls) == 1           # one clerk call for the one external recipient
        rows = await _rows()
        assert len(rows) == 2              # exactly one row per logical call
        assert {log_service.service_for_url(r.request_url) for r in rows} == {
            "ucsh_mailer", "clerk"
        }
        assert summary.outcome == "accepted"
        assert summary.succeeded == 2      # counts summed across services

    asyncio.run(flow())


def test_misconfigured_enabled_service_falls_back_to_smtp2go(monkeypatch):
    # Enabled but with no url/key: must not drop mail, must use smtp2go.
    monkeypatch.setattr(settings, "MAILER_ENABLED", True)
    monkeypatch.setattr(settings, "MAILER_URL", "")
    monkeypatch.setattr(settings, "MAILER_KEY", "")
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))

    async def flow():
        await _reset()
        await email_module.send_email(to=["staff@ucsh.com"], subject="S", html_body="<p>b</p>")
        (call,) = smtp.calls
        assert call["json"]["to"] == ["staff@ucsh.com"]  # the internal address went to smtp2go
        (row,) = await _rows()
        assert log_service.service_for_url(row.request_url) == "smtp2go"

    asyncio.run(flow())


def test_one_group_failing_still_attempts_the_others_and_raises_first(monkeypatch, no_sleep):
    _enable_mailer(monkeypatch)
    _enable_clerk(monkeypatch)
    _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    # Mailer (attempted first) fails hard; clerk (attempted after) also fails.
    m = _FakeHttp(_resp(400, {"error": "invalid_request", "message": "bad"}))
    c = _FakeHttp(_resp(401, {"error": "unauthorized", "message": "bad key"}))
    monkeypatch.setattr(mailer_client, "_http", m)
    monkeypatch.setattr(clerk_email, "_http", c)

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await email_module.send_email(
                to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>b</p>"
            )
        assert excinfo.value.response.status_code == 400  # the first failure, the mailer's
        assert len(c.calls) == 1                          # clerk still attempted after the mailer failed
        rows = await _rows()
        assert {r.outcome for r in rows} == {"http_error"}  # both failures recorded

    asyncio.run(flow())


def test_both_services_off_is_a_single_smtp2go_send(monkeypatch):
    smtp = _install_smtp2go(
        monkeypatch,
        _resp(200, {"request_id": "r", "data": {"succeeded": 2, "failed": 0, "email_id": "e"}}),
    )

    async def flow():
        await _reset()
        summary = await email_module.send_email(
            to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>b</p>",
            cc=["boss@ucaccess.com"],
        )
        # Everyone on one SMTP2GO call, exactly as before the new routing.
        (call,) = smtp.calls
        assert call["json"]["to"] == ["staff@ucsh.com", "jane@gmail.com"]
        assert call["json"]["cc"] == ["boss@ucaccess.com"]
        (row,) = await _rows()
        assert log_service.service_for_url(row.request_url) == "smtp2go"
        assert summary.outcome == "accepted" and summary.email_id == "e"

    asyncio.run(flow())


def test_a_group_with_only_cc_still_gets_the_mail(monkeypatch):
    # To is internal (mailer), the CC is outside and Clerk is off, so the CC is
    # alone on SMTP2GO; it must be sent there, not logged as "not attempted".
    _enable_mailer(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_mailer_accepted(recipients=1))
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        await email_module.send_email(
            to=["staff@ucsh.com"], subject="S", html_body="<p>b</p>", cc=["jane@gmail.com"],
        )
        (call,) = smtp.calls
        assert call["json"]["to"] == ["jane@gmail.com"]   # promoted from cc
        assert len(m.calls) == 1

    asyncio.run(flow())


# ----- the mailer refusing an address as not internal -----

def test_refused_addresses_reads_only_recipient_not_internal():
    refused = httpx.HTTPStatusError("x", request=httpx.Request("POST", "u"), response=_resp(
        400, {"error": "recipient_not_internal", "message": "m", "addresses": ["Temp@UCAccess.com"]}))
    other = httpx.HTTPStatusError("x", request=httpx.Request("POST", "u"), response=_resp(
        400, {"error": "invalid_request", "message": "m"}))
    assert mailer_client.refused_addresses(refused) == {"temp@ucaccess.com"}
    assert mailer_client.refused_addresses(other) == set()
    assert mailer_client.refused_addresses(ValueError("no response")) == set()


def test_refused_address_goes_the_outside_way_and_the_rest_resend(monkeypatch, no_sleep):
    # The app is set to treat ucaccess.com as internal, the mailer does not: the mailer
    # refuses the whole request, so the ucsh.com address is re-sent through the
    # mailer with a new key and the ucaccess.com one goes to SMTP2GO (Clerk off).
    _enable_mailer(monkeypatch)
    monkeypatch.setattr(settings, "INTERNAL_EMAIL_DOMAINS", "ucsh.com,ucaccess.com")
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp([
        _resp(400, {"error": "recipient_not_internal", "message": "m", "addresses": ["temp@ucaccess.com"]}),
        _mailer_accepted(recipients=1),
    ])
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        await email_module.send_email(
            to=["staff@ucsh.com"], subject="S", html_body="<p>b</p>", cc=["temp@ucaccess.com"],
        )
        first, second = m.calls
        assert first["json"]["to"] == ["staff@ucsh.com"] and first["json"]["cc"] == ["temp@ucaccess.com"]
        assert second["json"]["to"] == ["staff@ucsh.com"] and not second["json"].get("cc")
        assert second["headers"]["Idempotency-Key"] != first["headers"]["Idempotency-Key"]  # new body, new key
        (call,) = smtp.calls
        assert call["json"]["to"] == ["temp@ucaccess.com"]  # promoted from cc on the fallback

    asyncio.run(flow())


def test_refused_address_goes_to_clerk_when_clerk_is_on(monkeypatch, no_sleep):
    _enable_mailer(monkeypatch)
    _enable_clerk(monkeypatch)
    monkeypatch.setattr(settings, "INTERNAL_EMAIL_DOMAINS", "ucsh.com,ucaccess.com")
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_resp(400, {"error": "recipient_not_internal", "message": "m", "addresses": ["temp@ucaccess.com"]}))
    c = _FakeHttp(_clerk_accepted())
    monkeypatch.setattr(mailer_client, "_http", m)
    monkeypatch.setattr(clerk_email, "_http", c)

    async def flow():
        await _reset()
        await email_module.send_email(to=["temp@ucaccess.com"], subject="S", html_body="<p>b</p>")
        assert len(m.calls) == 1                     # nothing left for the mailer to resend
        assert len(c.calls) == 1 and smtp.calls == []

    asyncio.run(flow())


# ----- reply-to: the sending accounts have no inbox -----

def test_reply_to_is_the_callers_then_the_default_and_internal_only_for_the_mailer(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")
    assert email_module._resolve_reply_to(None) == (None, None)               # nothing set: no reply-to
    assert email_module._resolve_reply_to("boss@ucsh.com") == ("boss@ucsh.com", "boss@ucsh.com")
    # The mailer refuses an outside reply-to, so only smtp2go carries it.
    assert email_module._resolve_reply_to("me@gmail.com") == ("me@gmail.com", None)
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "hr@ucsh.com")
    assert email_module._resolve_reply_to("") == ("hr@ucsh.com", "hr@ucsh.com")  # default fills a blank
    assert email_module._resolve_reply_to("boss@ucsh.com")[0] == "boss@ucsh.com"  # the caller's wins


def test_send_email_passes_reply_to_to_both_services(monkeypatch):
    _enable_mailer(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_mailer_accepted(recipients=1))
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        await email_module.send_email(
            to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>hi</p>",
            reply_to="boss@ucsh.com",
        )
        (mailer_call,) = m.calls
        assert mailer_call["json"]["replyTo"] == "boss@ucsh.com"
        (smtp_call,) = smtp.calls
        assert {"header": "Reply-To", "value": "boss@ucsh.com"} in smtp_call["json"]["custom_headers"]

    asyncio.run(flow())


def test_no_reply_to_leaves_both_payloads_as_before(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")
    assert "replyTo" not in mailer_client.build_payload(["a@ucsh.com"], [], "S", "<p>b</p>")
    assert "custom_headers" not in email_module._build_payload(["a@x.com"], "S", "<p>b</p>", None, "Normal")
