"""Routing one send across SMTP2GO and the UCSH mailer.

These cover the per-recipient routing in ``app/graph/email.py`` and the mailer
client. The guarantees under test: each recipient reaches the service its
domain designates (UCSH -> mailer while it is on, everything else -> SMTP2GO);
the client builds the right request with its key in a header; it retries only
what it is allowed to, with the same idempotency key, and leaves exactly one
redacted log row per logical call; a misconfigured but enabled mailer falls
back to SMTP2GO; one failing group does not stop the other and the first
failure is raised; and with the mailer off the behaviour and logging are
unchanged.
"""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import Base, async_session, engine
from app.graph import email as email_module, mailer_client
from app.models import EmailApiLog, EmailApiLogRecipient
from app.services import email_api_log as log_service
from app import templates_render
from app.templates_render import LAYOUT_MARKER, render_layout


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
    """Make the mailer client's retry backoff instant."""
    async def _instant(_seconds):
        pass

    monkeypatch.setattr(mailer_client, "_sleep", _instant)


def _mailer_accepted(status="sent", recipients=1, message_ids=("m1",)):
    return _resp(200, {
        "status": status,
        "recipients": recipients,
        "messages": [{"messageId": mid, "recipients": ["x@ucsh.com"]} for mid in message_ids],
    })


def _enable_mailer(monkeypatch):
    monkeypatch.setattr(settings, "MAILER_ENABLED", True)
    monkeypatch.setattr(settings, "MAILER_URL", "https://mailer.internal")
    monkeypatch.setattr(settings, "MAILER_KEY", "mk_secret")
    monkeypatch.setattr(settings, "MAILER_FROM", "ooo@ucsh.com")


def _install_smtp2go(monkeypatch, response):
    """Point the SMTP2GO path at a fake and skip its real rate limiter."""
    fake = _FakeHttp(response)
    monkeypatch.setattr(email_module, "_http", fake)

    async def _no_wait():
        pass

    monkeypatch.setattr(email_module, "_rate_limit", _no_wait)
    return fake


# ----- routing by domain for each switch setting -----

def test_routing_sends_everything_to_smtp2go_when_the_mailer_is_off():
    smtp, mailer = email_module._route_recipients(
        ["staff@ucsh.com", "temp@ucaccess.com"], ["out@gmail.com"]
    )
    assert smtp == {"to": ["staff@ucsh.com", "temp@ucaccess.com"], "cc": ["out@gmail.com"]}
    assert mailer == {"to": [], "cc": []}


def test_routing_internal_to_mailer_external_to_smtp2go_when_the_mailer_is_on(monkeypatch):
    _enable_mailer(monkeypatch)
    smtp, mailer = email_module._route_recipients(
        ["staff@ucsh.com", "out@gmail.com"], ["boss@ucsh.com", "temp@ucaccess.com"]
    )
    assert mailer == {"to": ["staff@ucsh.com"], "cc": ["boss@ucsh.com"]}
    # Only ucsh.com is internal by default: ucaccess.com stays on smtp2go like any outside address.
    assert smtp == {"to": ["out@gmail.com"], "cc": ["temp@ucaccess.com"]}


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


# ----- end to end through send_email -----

def test_send_email_splits_between_mailer_and_smtp2go(monkeypatch):
    _enable_mailer(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_mailer_accepted(recipients=1))
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        summary = await email_module.send_email(
            to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>hi</p>"
        )
        (call,) = smtp.calls
        assert call["json"]["to"] == ["jane@gmail.com"]  # the outside address stayed on smtp2go
        assert len(m.calls) == 1           # one mailer call for the internal group
        rows = await _rows()
        assert len(rows) == 2              # exactly one row per logical call
        assert {log_service.service_for_url(r.request_url) for r in rows} == {
            "ucsh_mailer", "smtp2go"
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


def test_one_group_failing_still_attempts_the_other_and_raises(monkeypatch, no_sleep):
    _enable_mailer(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    # The mailer fails hard; the smtp2go group is still sent.
    m = _FakeHttp(_resp(400, {"error": "invalid_request", "message": "bad"}))
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await email_module.send_email(
                to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>b</p>"
            )
        assert excinfo.value.response.status_code == 400  # the mailer's failure is raised
        assert len(smtp.calls) == 1                       # smtp2go still sent its group
        rows = await _rows()
        assert {r.outcome for r in rows} == {"http_error", "accepted"}  # both calls recorded

    asyncio.run(flow())


def test_mailer_off_is_a_single_smtp2go_send(monkeypatch):
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
    # To is internal (mailer) and the CC is outside, so the CC is
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


def test_refused_address_goes_to_smtp2go_and_the_rest_resend(monkeypatch, no_sleep):
    # The app is set to treat ucaccess.com as internal, the mailer does not:
    # the mailer refuses the whole request, so the ucsh.com address is re-sent
    # through the mailer with a new key and the ucaccess.com one goes to SMTP2GO.
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


# ----- the one shared layout, applied centrally in send_email -----

def test_render_layout_wraps_once_and_is_idempotent():
    once = render_layout("<p>hi</p>")
    assert once.lstrip().startswith("<!DOCTYPE html>")   # a full document, header band and all
    assert once.count(LAYOUT_MARKER) == 1                # wrapped exactly once
    assert "<p>hi</p>" in once                           # the content is carried through
    # An already-wrapped body is returned untouched, so a second wrap is a no-op.
    assert render_layout(once) == once
    assert render_layout(once).count("<!DOCTYPE html>") == 1


def test_send_email_wraps_the_body_in_the_layout_for_both_services(monkeypatch):
    _enable_mailer(monkeypatch)
    smtp = _install_smtp2go(monkeypatch, _resp(200, {"data": {"succeeded": 1, "failed": 0}}))
    m = _FakeHttp(_mailer_accepted(recipients=1))
    monkeypatch.setattr(mailer_client, "_http", m)

    async def flow():
        await _reset()
        await email_module.send_email(
            to=["staff@ucsh.com", "jane@gmail.com"], subject="S", html_body="<p>hi</p>",
        )
        smtp_html = smtp.calls[0]["json"]["html_body"]   # what SMTP2GO was sent
        mailer_html = m.calls[0]["json"]["html"]         # what the mailer was sent
        for body in (smtp_html, mailer_html):
            assert LAYOUT_MARKER in body                 # every service got the shell
            assert body.count("<!DOCTYPE html>") == 1    # and only one shell
            assert "<p>hi</p>" in body                   # with the original content inside

    asyncio.run(flow())


def test_button_macro_emits_both_the_vml_and_the_non_mso_anchor():
    html = templates_render._env.from_string(
        "{% import '_macros.html' as ui %}{{ ui.button('https://x.test/go', 'Approve', '#15803d') }}"
    ).render()
    assert "<!--[if mso]>" in html and "v:roundrect" in html        # the Outlook (VML) button
    assert "<!--[if !mso]><!-->" in html                            # the everyone-else branch
    assert '<a href="https://x.test/go"' in html                    # the real anchor
    assert "#15803d" in html                                        # painted the colour it was given


def test_dashboard_footer_keeps_its_links_and_its_warning():
    links = [
        {"label": "My Dashboard", "url": "https://x.test/emp"},
        {"label": "Team Dashboard", "url": "https://x.test/mgr"},
    ]
    html = templates_render.render_dashboard_footer(links)
    for link in links:                                              # every dashboard still linked
        assert link["url"] in html and link["label"] in html
    assert "v:roundrect" in html                                    # Outlook-safe buttons, not thin links
    assert "Do not forward this email" in html                     # the standing warning stays
    assert "cryptographed" in html
