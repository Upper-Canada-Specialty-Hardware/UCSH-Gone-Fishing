"""Every reject carries a reason (#433), and a rejection email takes one of two
forms (#434).

These cover the reason end to end without a real backend: the SMS parser (pure),
the two dashboard routes' "a reason is required" 400 and their pass-through, the
email approval page asking again when a reject has no reason, and the two
rejection-email forms (supervisor and HR) built by the decision path's
``_result_context``. Secrets are stubbed by conftest; no network is touched.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.models.request_record import RequestRecord
from app.routes import dashboard
from app.routes.dashboard import DashboardUser, RejectBody
from app.services import request_decisions as rd


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


async def _noop(*args, **kwargs):
    """An async stand-in for a pre-check that should do nothing in the test."""
    return None


# ----- SMS parsing: words after the number are the reason (#433) -----


def test_sms_reject_with_words_keeps_them_as_the_reason():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, request_type, reason = _parse_sms_body("OT Reject 45 Too Busy This Week")
    assert decision == "Reject" and item_id == "45" and request_type == "overtime"
    assert reason == "Too Busy This Week"             # original case kept


def test_sms_reject_without_words_has_no_reason():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, request_type, reason = _parse_sms_body("LR Reject 12")
    assert decision == "Reject" and item_id == "12" and request_type == "leave"
    assert reason is None                             # no trailing words means no reason


def test_sms_approve_has_no_reason():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, request_type, reason = _parse_sms_body("CO Approve 7")
    assert decision == "Approve" and item_id == "7" and request_type == "carryover-payout"
    assert reason is None


# ----- the decision comes from BEFORE the number, the reason from after (finding 1) -----


def test_sms_reject_reason_mentioning_approve_stays_a_reject():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, request_type, reason = _parse_sms_body(
        "LR Reject 123 can't approve, short staffed"
    )
    assert decision == "Reject" and item_id == "123" and request_type == "leave"
    assert reason == "can't approve, short staffed"   # the word "approve" in the reason never flips the decision


@pytest.mark.parametrize("reason_words", ["approved next time", "disapprove", "approve not"])
def test_sms_reject_reason_words_do_not_flip_the_decision(reason_words):
    from app.routes.twilio import _parse_sms_body
    decision, item_id, _rt, reason = _parse_sms_body(f"OT Reject 45 {reason_words}")
    assert decision == "Reject" and item_id == "45"
    assert reason == reason_words                     # kept verbatim, decision stays Reject


def test_sms_disapprove_word_is_a_reject():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, _rt, reason = _parse_sms_body("OT Disapprove 5")
    assert decision == "Reject" and item_id == "5"    # "disapprove" (contains "approve") still means reject
    assert reason is None


def test_sms_reject_glued_to_the_number_is_still_a_reject():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, _rt, reason = _parse_sms_body("LR Reject12 covered already")
    assert decision == "Reject" and item_id == "12"   # no space before the number
    assert reason == "covered already"


def test_sms_approve_messages_are_unchanged():
    from app.routes.twilio import _parse_sms_body
    decision, item_id, request_type, reason = _parse_sms_body("LR Approve 123")
    assert (decision, item_id, request_type, reason) == ("Approve", "123", "leave", None)


def test_sms_reject_reason_is_capped_at_the_entry_limit():
    from app.routes.twilio import _parse_sms_body
    from app.services.request_decisions import MAX_REJECT_REASON_LEN
    long_reason = "x" * (MAX_REJECT_REASON_LEN + 50)
    _decision, _item_id, _rt, reason = _parse_sms_body(f"LR Reject 9 {long_reason}")
    assert reason is not None and len(reason) == MAX_REJECT_REASON_LEN


# ----- the unparseable-reply help text shows the optional reject reason (finding 2) -----


def test_the_unrecognized_sms_reply_shows_the_optional_reject_reason():
    from app.routes.twilio import UNRECOGNIZED_SMS_REPLY
    assert "<reason>" in UNRECOGNIZED_SMS_REPLY        # a reason can follow the number
    assert "optional" in UNRECOGNIZED_SMS_REPLY.lower()  # and a bare reject still works


# ----- the dashboard routes: a reason is required, and it is passed on (#433) -----


def test_team_reject_without_a_reason_is_a_400(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)            # not mid-switch
    monkeypatch.setattr(dashboard, "_refuse_manager_not_on_request", _noop)  # pass the own-team gate
    user = DashboardUser(role="manager", user_id="42")
    with pytest.raises(HTTPException) as err:
        run(dashboard.team_reject(user, "leave", "7", RejectBody(reason="   ")))   # blank
    assert err.value.status_code == 400 and "reason is required" in err.value.detail.lower()


def test_admin_reject_without_a_reason_is_a_400(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    with pytest.raises(HTTPException) as err:
        run(dashboard.admin_reject("leave", "7", RejectBody()))           # missing reason -> default ""
    assert err.value.status_code == 400 and "reason is required" in err.value.detail.lower()


def test_admin_reject_with_no_body_at_all_is_a_400(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    with pytest.raises(HTTPException) as err:
        run(dashboard.admin_reject("leave", "7", None))               # no body at all -> the 400, not a 422
    assert err.value.status_code == 400 and "reason is required" in err.value.detail.lower()


def test_team_reject_with_too_long_a_reason_is_a_400(monkeypatch):
    from app.services.request_decisions import MAX_REJECT_REASON_LEN
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    monkeypatch.setattr(dashboard, "_refuse_manager_not_on_request", _noop)
    user = DashboardUser(role="manager", user_id="42")
    with pytest.raises(HTTPException) as err:
        run(dashboard.team_reject(user, "leave", "7", RejectBody(reason="x" * (MAX_REJECT_REASON_LEN + 1))))
    assert err.value.status_code == 400 and str(MAX_REJECT_REASON_LEN) in err.value.detail


def test_admin_reject_with_too_long_a_reason_is_a_400(monkeypatch):
    from app.services.request_decisions import MAX_REJECT_REASON_LEN
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    with pytest.raises(HTTPException) as err:
        run(dashboard.admin_reject("leave", "7", RejectBody(reason="x" * (MAX_REJECT_REASON_LEN + 1))))
    assert err.value.status_code == 400 and str(MAX_REJECT_REASON_LEN) in err.value.detail


def test_team_reject_passes_the_trimmed_reason_to_the_handler(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    monkeypatch.setattr(dashboard, "_refuse_manager_not_on_request", _noop)
    seen = {}

    async def _handler(request_id, actor, channel, link_version=None, reason=None):
        seen.update(actor=actor, channel=channel, link_version=link_version, reason=reason)
        return {"status": "rejected"}

    monkeypatch.setitem(dashboard.HANDLERS, ("leave", "reject"), _handler)
    user = DashboardUser(role="manager", user_id="42")
    result = run(dashboard.team_reject(user, "leave", "7", RejectBody(reason="  too busy  ")))
    assert result == {"status": "rejected"}
    assert seen == {"actor": "42", "channel": "manager_dashboard", "link_version": None, "reason": "too busy"}


def test_admin_reject_passes_the_trimmed_reason_to_the_handler(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    monkeypatch.setattr(dashboard, "_refuse_if_paused", _noop)
    seen = {}

    async def _handler(request_id, actor, channel, link_version=None, reason=None):
        seen.update(actor=actor, channel=channel, link_version=link_version, reason=reason)
        return {"status": "rejected"}

    monkeypatch.setitem(dashboard.HANDLERS, ("leave", "reject"), _handler)
    result = run(dashboard.admin_reject("leave", "7", RejectBody(reason=" policy ")))
    assert result == {"status": "rejected"}
    assert seen["channel"] == "admin_dashboard" and seen["reason"] == "policy"
    assert seen["actor"] == dashboard.ADMIN_DEFAULT_ID and seen["link_version"] is None


# ----- the email approval page asks again when a reject has no reason (#433) -----


def _approval_client(monkeypatch):
    """A test client over the approval router, processing on and the link current."""
    from app.routes import approval as approval_route
    from app.routes.approval import router as approval_router
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def _same_version(list_id, request_id):
        return 1

    monkeypatch.setattr(approval_route, "get_current_version", _same_version)
    app = FastAPI()
    app.include_router(approval_router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False), approval_route


def _reject_form(reason=None):
    """The POST form a reject confirm page submits (optionally with a reason)."""
    from app.services.approval_links import NO_EXPIRY, _sign
    token = _sign("leave", "3402", "reject", "42", str(NO_EXPIRY), 1)
    data = {"token": token, "mgr": "42", "exp": str(NO_EXPIRY), "v": "1"}
    if reason is not None:
        data["reason"] = reason
    return data


def test_the_email_page_asks_again_when_a_reject_has_no_reason(monkeypatch):
    client, _ = _approval_client(monkeypatch)
    resp = client.post("/api/leave/reject/3402", data=_reject_form())      # no reason field
    assert resp.status_code == 200, resp.text
    assert "Please enter a reason" in resp.text                            # the friendly ask-again note
    assert "Confirm Rejection" in resp.text                                # it re-showed the confirm page


def test_the_email_page_asks_again_when_a_reject_reason_is_too_long(monkeypatch):
    from app.services.request_decisions import MAX_REJECT_REASON_LEN
    client, _ = _approval_client(monkeypatch)
    resp = client.post("/api/leave/reject/3402", data=_reject_form("y" * (MAX_REJECT_REASON_LEN + 1)))
    assert resp.status_code == 200, resp.text
    assert str(MAX_REJECT_REASON_LEN) in resp.text                         # the length note
    assert "Confirm Rejection" in resp.text                                # it re-showed the confirm page


def test_the_email_page_rejects_with_a_reason(monkeypatch):
    client, approval_route = _approval_client(monkeypatch)
    seen = {}

    async def _handler(request_id, mgr, channel, link_version=None, reason=None):
        seen.update(channel=channel, reason=reason)
        return {"status": "rejected"}

    monkeypatch.setitem(approval_route.HANDLERS, ("leave", "reject"), _handler)
    resp = client.post("/api/leave/reject/3402", data=_reject_form("not this week"))
    assert resp.status_code == 200, resp.text
    assert "rejected" in resp.text.lower()                                 # the rejected result page
    assert seen == {"channel": "email_link", "reason": "not this week"}


# ----- the two rejection-email forms, each with the reason (#434) -----


def _rejected_record(**overrides) -> RequestRecord:
    """A transient leave request with just the fields the result builder reads."""
    base = dict(
        kind="leave", item_id=7, employee_name="Sam Staff",
        decided_by="99", decided_channel="manager_dashboard", manager_name="Dana Lee",
    )
    base.update(overrides)
    return RequestRecord(**base)


def test_supervisor_rejection_names_the_manager_and_the_reason():
    record = _rejected_record()                                            # a manager decided
    _, html, sms = rd._result_context(record, "Rejected", [], "ignored", reason="too busy")
    assert "Dana Lee did not approve your leave request" in html
    assert "Reason:" in html and "too busy" in html
    assert "contact HR" not in html                                        # the HR line is admin-only
    assert "Dana Lee did not approve your leave request #7." in sms
    assert "Reason: too busy" in sms


def test_admin_rejection_says_HR_and_adds_the_contact_line():
    record = _rejected_record(decided_by="admin", decided_channel="admin_dashboard")
    _, html, sms = rd._result_context(record, "Rejected", [], "ignored", reason="policy")
    assert "HR did not approve your leave request" in html
    assert "Reason:" in html and "policy" in html
    assert "If you have questions, contact HR." in html
    assert "HR did not approve your leave request #7." in sms
    assert "If you have questions, contact HR." in sms


def test_rejection_with_no_reason_falls_back_to_the_plain_sentence():
    record = _rejected_record()
    _, html, sms = rd._result_context(
        record, "Rejected", [], "The request was not approved.", reason=None,
    )
    assert "was not approved" in html                                      # today's wording
    assert "Reason:" not in html                                           # no reason line
    assert "The request was not approved." in sms


# ----- the result SMS stays within Twilio's limit; the email keeps the full reason (finding 14) -----


def test_truncate_sms_caps_at_the_twilio_limit():
    short = "within the limit"
    assert rd._truncate_sms(short) == short                                # unchanged when it fits
    long = "z" * (rd._SMS_BODY_MAX + 500)
    capped = rd._truncate_sms(long)
    assert len(capped) == rd._SMS_BODY_MAX and capped.endswith("...")      # trimmed and marked


def test_a_long_rejection_reason_truncates_the_sms_but_not_the_email():
    record = _rejected_record()
    long_reason = "q" * (rd._SMS_BODY_MAX + 1000)                          # far over Twilio's limit
    _, html, sms = rd._result_context(record, "Rejected", [], "ignored", reason=long_reason)
    assert len(sms) <= rd._SMS_BODY_MAX                                    # the text is kept sendable
    assert long_reason in html                                            # the email keeps the whole reason


# ----- the timeline line shows a rejected event's reason (#433) -----


def test_timeline_line_shows_a_rejected_events_reason():
    from app.routes.events import _timeline_line
    event = SimpleNamespace(type="rejected", channel="manager_dashboard", actor="Dana",
                            detail={"reason": "too busy"})
    assert "(reason: too busy)" in _timeline_line(event)


def test_timeline_line_has_no_reason_clause_without_one():
    from app.routes.events import _timeline_line
    event = SimpleNamespace(type="rejected", channel="sms", actor="Dana", detail={})
    assert "reason:" not in _timeline_line(event)
