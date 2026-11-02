"""First-week messaging fixes for the Postgres path (#416-#427).

These cover the outbox and submission changes that make manager messaging behave
once requests live in Postgres: phone numbers are normalised and masked, a
manager ask is not sent once a request is decided, a permanent provider failure
is not retried forever and leaves one timeline line, reprocess re-asks over both
channels without touching the reminder cadence, the reminder subject is prefixed,
a pending request's stage is derived live, the admin table marks failed sends,
the "manager asked" record reads the outbox, and a bereavement/jury alert is
queued through the outbox exactly once. The email and SMS senders are stubbed, so
nothing leaves the test.
"""
import asyncio
import itertools
import types
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.models import RequestApprovalState
from app.models.mixins import utcnow
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.services import request_submission as rsub
from app.services.outbox_sender import send_outbox_row
from app.services.request_journal import step_key
from app.services.request_threads import thread_subject
from app.services.sms import (
    InvalidPhoneNumber,
    mask_phone,
    mask_phone_numbers,
    normalize_phone,
    scrub_error_text,
    scrub_twilio_sid,
)

# Fake Twilio account ids, built at runtime so no literal id sits in the repo
# (a literal trips the secret scanner on push).
FAKE_SID = "AC" + "0123456789abcdef" * 2
FAKE_SID_2 = "AC" + "deadbeef" * 4

_item_ids = itertools.count(94_000_000)


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean():
    """Wipe the request and outbox tables before and after each test."""
    async def wipe():
        async with async_session() as session:
            for model in (Outbox, RequestEvent, RequestChange, RequestRecord,
                          RequestCounter, RequestApprovalState):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


async def _leave(**overrides) -> RequestRecord:
    """Insert one leave request row and return it, with sensible defaults."""
    values = dict(
        kind="leave", item_id=next(_item_ids), status="pending",
        employee_id="7", employee_email="casey@ucsh.com", employee_name="Casey Demo",
        leave_type="Bereavement", start_date=date(2026, 11, 2), end_date=date(2026, 11, 2),
        manager_id="5", manager_name="Morgan Boss", approval_version=1, balance_state="not_applied",
    )
    values.update(overrides)
    async with async_session() as session:
        record = RequestRecord(**values)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record


# ===== #417: phone numbers reduce to digits, not the last ten characters =====


@pytest.mark.parametrize("raw, expected", [
    ("(416) 949-9131", "+14169499131"),             # formatted local number: the old bug produced "+1) 949-9131"
    ("4169499131", "+14169499131"),                 # bare ten digits
    ("416-949-9131", "+14169499131"),               # dashed ten digits
    ("14169499131", "+14169499131"),                # 1 + ten digits
    ("+1 (416) 949-9131", "+14169499131"),          # already +, keep + and digits
    ("+447911123456", "+447911123456"),             # international +, kept as given
])
def test_normalize_phone_reduces_to_digits(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "12345", "949-9131", "+"])
def test_normalize_phone_raises_on_an_unusable_number(bad):
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(bad)


# ===== #420: mask a number, and scrub the Twilio Account SID from stored text =====


def test_mask_phone_keeps_only_the_last_four_digits():
    assert mask_phone("(416) 949-9131") == "***9131"
    assert mask_phone("+14169499131") == "***9131"
    assert mask_phone("123") == "****"               # too short to reveal anything
    assert mask_phone("") == "****"


def test_scrub_twilio_sid_replaces_the_account_sid(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "" + FAKE_SID_2 + "")
    # The configured SID, verbatim.
    assert scrub_twilio_sid("url .../" + FAKE_SID_2 + "/Messages") == "url .../AC****/Messages"
    # Any AC + 32 hex token, even a different one in the error URL.
    assert scrub_twilio_sid("GET /Accounts/" + FAKE_SID + "/x") == "GET /Accounts/AC****/x"
    assert scrub_twilio_sid(None) is None


def test_mask_phone_numbers_hides_a_number_in_error_text():
    # Twilio quotes the recipient in its 4xx message; the stored error must not.
    text = "The 'To' number +14169499131 is not a valid phone number."
    masked = mask_phone_numbers(text)
    assert "+14169499131" not in masked and "4169499131" not in masked
    assert "***9131" in masked
    assert mask_phone_numbers(None) is None


def test_mask_phone_numbers_leaves_short_digit_runs_alone():
    # Fewer than seven digits is nothing worth hiding (an error code, a count).
    assert mask_phone_numbers("code 21211 failed") == "code 21211 failed"


def test_scrub_error_text_masks_both_the_sid_and_the_number():
    text = ("POST /Accounts/" + FAKE_SID + "/Messages.json "
            "The 'To' number +14169499131 is not valid.")
    scrubbed = scrub_error_text(text)
    assert "" + FAKE_SID + "" not in scrubbed and "AC****" in scrubbed
    assert "4169499131" not in scrubbed and "***9131" in scrubbed


def test_invalid_phone_number_message_does_not_leak_the_raw_number():
    with pytest.raises(InvalidPhoneNumber) as err:
        normalize_phone("12345678")                     # eight digits: not a mappable shape
    assert "12345678" not in str(err.value) and "***5678" in str(err.value)


# ===== helpers for sending one outbox row =====


def _stub_email(monkeypatch, error=None, result=None):
    """Stub app.graph.email.send_email; count calls; optionally raise."""
    calls = []

    async def fake_send(recipients, subject, body):
        calls.append((tuple(recipients), subject))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr("app.graph.email.send_email", fake_send)
    return calls


def _stub_sms(monkeypatch, error=None):
    """Stub app.services.sms.send_sms; count calls; optionally raise."""
    calls = []

    async def fake_send(to, body, **kwargs):      # **kwargs: send_sms now takes text-log context (#494)
        calls.append(to)
        if error is not None:
            raise error
        return {"sid": "SM-1"}

    monkeypatch.setattr("app.services.sms.send_sms", fake_send)
    return calls


async def _enqueue(record, *, channel="email", message_type="result", recipient="casey@ucsh.com",
                   key=None, version=1) -> int:
    """Insert one "sending" outbox row for a request and return its id."""
    async with async_session() as session:
        row = Outbox(
            request_id=record.id, message_type=message_type, channel=channel, recipient=recipient,
            subject="Subject" if channel == "email" else None, body="body",
            status="sending", attempts=0,
            idempotency_key=key or f"{step_key(record.kind, record.item_id, 'sent_to_manager', version)}:{channel}:{recipient}",
        )
        session.add(row)
        await session.commit()
        return row.id


async def _deliver(row_id):
    """Send one outbox row through the real sender and commit."""
    async with async_session() as session:
        row = await session.get(Outbox, row_id)
        await send_outbox_row(session, row)
        await session.commit()


async def _row_and_events(row_id, request_id):
    async with async_session() as session:
        row = await session.get(Outbox, row_id)
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == request_id)
        )).scalars().all()
        return row, events


def _http_error(status, *, json=None, text=None):
    """Build an httpx.HTTPStatusError with a response carrying a SID in its URL."""
    url = "https://api.twilio.com/2010-04-01/Accounts/" + FAKE_SID + "/Messages.json"
    request = httpx.Request("POST", url)
    response = httpx.Response(status, json=json, text=text, request=request)
    return httpx.HTTPStatusError("http error", request=request, response=response)


# ===== #419: a permanent failure is not retried; the error keeps code+message =====


def test_a_4xx_fails_at_once_and_keeps_the_service_code_and_message(monkeypatch):
    _stub_email(monkeypatch, error=_http_error(400, json={"code": 21211, "message": "'To' invalid"}))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com"))

    run(_deliver(row_id))

    row, events = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.attempts == 1
    assert row.next_attempt_at is None               # permanent: no retry scheduled
    assert "21211" in row.last_error and "'To' invalid" in row.last_error
    assert [e.type for e in events].count("send_failed") == 1


def test_a_permanent_failure_scrubs_the_twilio_sid_from_the_stored_error(monkeypatch):
    _stub_sms(monkeypatch, error=_http_error(400, text="sent to /Accounts/" + FAKE_SID + "/"))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, channel="sms", message_type="manager_approval", recipient="+14169499131"))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is None
    assert "" + FAKE_SID + "" not in row.last_error
    assert "AC****" in row.last_error


def test_a_permanent_failure_masks_the_phone_number_in_the_stored_error(monkeypatch):
    _stub_sms(monkeypatch, error=_http_error(
        400, json={"code": 21211, "message": "The 'To' number +14169499131 is not a valid phone number."},
    ))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, channel="sms", message_type="manager_approval", recipient="+14169499131"))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is None
    assert "4169499131" not in row.last_error            # the recipient number never lands in the stored error
    assert "***9131" in row.last_error


@pytest.mark.parametrize("status", [401, 402, 403])
def test_an_account_level_4xx_keeps_the_backoff(monkeypatch, status):
    # 401 bad credentials, 402 Twilio out of balance (the July/Aug outage), 403:
    # account-level and fixable, so the row must retry, not fail at once (finding 7).
    _stub_email(monkeypatch, error=_http_error(status, json={"code": status, "message": "account"}))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com"))

    run(_deliver(row_id))

    row, events = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is not None   # transient: backoff kept
    assert not any(e.type == "send_failed" for e in events)             # no final-failure row yet


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_recipient_level_4xx_fails_at_once(monkeypatch, status):
    # A bad request / missing resource / unprocessable entity is about the
    # recipient or the content, so retrying is pointless (finding 7).
    _stub_email(monkeypatch, error=_http_error(status, json={"code": status, "message": "bad"}))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com"))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is None       # permanent: no retry


def test_an_invalid_phone_number_is_a_permanent_failure(monkeypatch):
    _stub_sms(monkeypatch, error=InvalidPhoneNumber("cannot normalise"))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, channel="sms", message_type="manager_approval", recipient="garbage"))

    run(_deliver(row_id))

    row, events = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is None and row.attempts == 1
    assert [e.type for e in events].count("send_failed") == 1


def test_a_429_keeps_the_backoff_and_adds_no_timeline_row(monkeypatch):
    _stub_email(monkeypatch, error=_http_error(429, json={"code": 20429, "message": "Too Many Requests"}))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com"))

    run(_deliver(row_id))

    row, events = run(_row_and_events(row_id, record.id))
    assert row.status == "failed" and row.next_attempt_at is not None   # rate limiting is transient
    assert not any(e.type == "send_failed" for e in events)             # only the final failure records one


def test_the_worker_never_reclaims_a_permanently_failed_row(monkeypatch):
    from app.tasks import outbox_worker
    _stub_email(monkeypatch, error=_http_error(404, json={"code": 20404, "message": "Not Found"}))
    record = run(_leave(status="pending"))
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com"))

    run(_deliver(row_id))                            # fails permanently, well below the attempt cap
    claimed = run(outbox_worker._claim_due(10))

    assert row_id not in claimed                     # failed + no next attempt = terminal


# ===== #418: a manager ask is dropped once the request is decided or superseded =====


def test_a_manager_ask_for_a_decided_request_is_dropped_unsent(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="X"))
    record = run(_leave(status="approved"))          # already decided
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com", version=1))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "dropped" and calls == []   # nothing sent
    assert row.attempts == 0                          # not counted as an attempt


def test_a_manager_ask_for_a_superseded_version_is_dropped_unsent(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="X"))
    record = run(_leave(status="pending", approval_version=2))        # an admin edit bumped it to v2
    row_id = run(_enqueue(record, message_type="manager_approval", recipient="mgr@ucsh.com", version=1))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "dropped" and calls == []   # the v1 links are stale


def test_a_reminder_for_a_still_pending_current_request_still_sends(monkeypatch):
    calls = _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="X"))
    record = run(_leave(status="pending", approval_version=1))
    key = f"{step_key('leave', record.item_id, 'reminder_sent', 1, suffix='2026-11-02')}:email:mgr@ucsh.com"
    row_id = run(_enqueue(record, message_type="reminder", recipient="mgr@ucsh.com", key=key))

    run(_deliver(row_id))

    row, _ = run(_row_and_events(row_id, record.id))
    assert row.status == "sent" and len(calls) == 1


# ===== #422 / #520: a Postgres reminder keeps the request's one subject =====


_EMPLOYEE = {"id": "7", "fields": {"Title": "Casey Demo", "EmailAddress": "casey@ucsh.com"}}
_MANAGER = {"id": "5", "fields": {"Title": "Morgan Boss", "EmailAddress": "mgr@ucsh.com",
                                  "CellNumber": "+15555550101"}}


async def _enqueue_via(record, *, suffix=None, reask_suffix=None):
    async with async_session() as session:
        await rsub._enqueue_notifications(
            session, record, _EMPLOYEE, [_MANAGER], event=None,
            suffix=suffix, include_receipt=False, reask_suffix=reask_suffix,
        )
        await session.commit()
    async with async_session() as session:
        return (await session.execute(
            select(Outbox).where(Outbox.request_id == record.id)
        )).scalars().all()


def test_a_postgres_reminder_keeps_the_request_subject_and_says_reminder_in_the_body():
    record = run(_leave(status="pending"))
    rows = run(_enqueue_via(record, suffix="2026-11-02"))     # a reminder (dated)
    assert all(r.channel == "email" for r in rows)            # reminders are email-only
    # Same subject as the first ask, so it threads; the body says it is a reminder (#520).
    assert all(r.subject == thread_subject(record) for r in rows)
    assert all(r.body.startswith(rsub.REMINDER_NOTE) for r in rows)


def test_a_first_manager_ask_has_no_reminder_line_and_a_text():
    record = run(_leave(status="pending"))
    rows = run(_enqueue_via(record))                          # a first ask (suffix None)
    channels = sorted(r.channel for r in rows)
    assert channels == ["email", "sms"]                       # email and a text
    email = next(r for r in rows if r.channel == "email")
    assert email.subject == thread_subject(record)
    assert "Reminder:" not in email.body


# ===== #421: reprocess re-asks over both channels, every press, no cadence bump =====


def _stub_routing(monkeypatch):
    """Point reprocess/reminder routing at one employee and one available manager."""
    async def resolve(record, fields):
        return _EMPLOYEE

    async def managers(employee):
        return [_MANAGER]

    async def choose(session, mgrs, on_date):
        return _MANAGER, []

    monkeypatch.setattr(rsub, "_resolve_employee", resolve)
    monkeypatch.setattr(rsub, "get_all_managers_for_employee", managers)
    monkeypatch.setattr(rsub, "choose_available_manager", choose)


def test_reprocess_sends_both_channels_every_press_without_touching_the_cadence(monkeypatch):
    _stub_routing(monkeypatch)
    record = run(_leave(status="pending"))

    assert run(rsub.reprocess_request("leave", record.item_id, "resend #1")) is True
    assert run(rsub.reprocess_request("leave", record.item_id, "resend #2")) is True

    async def read():
        async with async_session() as session:
            rows = (await session.execute(
                select(Outbox).where(Outbox.request_id == record.id)
            )).scalars().all()
            events = (await session.execute(
                select(RequestEvent).where(
                    RequestEvent.request_id == record.id, RequestEvent.type == "sent_to_manager"
                )
            )).scalars().all()
            fresh = await session.get(RequestRecord, record.id)
            return rows, events, fresh

    rows, events, fresh = run(read())
    # Two presses, each one email + one text = four outbox rows, none a reminder.
    assert len(rows) == 4
    assert sorted(r.channel for r in rows) == ["email", "email", "sms", "sms"]
    assert not any("Reminder:" in r.body for r in rows if r.channel == "email")
    assert len(events) == 2                                   # one timeline step per press
    assert any(e.detail.get("reason") == "resend #2" for e in events)
    assert not fresh.reminder_count                           # the cadence did not advance (0 or None)
    assert fresh.last_reminder_at is None


def test_reprocess_does_nothing_when_every_manager_is_off(monkeypatch):
    async def resolve(record, fields):
        return _EMPLOYEE

    async def managers(employee):
        return [_MANAGER]

    async def all_off(session, mgrs, on_date):
        return None, [{"manager": "Morgan Boss", "back": "2026-11-09"}]

    monkeypatch.setattr(rsub, "_resolve_employee", resolve)
    monkeypatch.setattr(rsub, "get_all_managers_for_employee", managers)
    monkeypatch.setattr(rsub, "choose_available_manager", all_off)
    record = run(_leave(status="pending"))

    assert run(rsub.reprocess_request("leave", record.item_id, "resend")) is False

    async def count():
        async with async_session() as session:
            return len((await session.execute(
                select(Outbox).where(Outbox.request_id == record.id)
            )).scalars().all())

    assert run(count()) == 0


# ===== #416: the "manager asked" record reads the outbox on the Postgres path =====


def test_fetch_manager_asked_records_reads_the_outbox_and_the_old_table():
    from app.services.employee_validation import fetch_manager_asked_records

    asked = run(_leave(status="pending"))                    # app-native, a manager_approval row exists
    not_asked = run(_leave(status="pending"))                # app-native, nothing queued
    imported = run(_leave(status="pending", sp_item_id=555)) # imported, old approval-state row

    async def seed():
        async with async_session() as session:
            session.add(Outbox(
                request_id=asked.id, message_type="manager_approval", channel="email",
                recipient="mgr@ucsh.com", body="b", status="pending", attempts=0,
                idempotency_key=f"{step_key('leave', asked.item_id, 'sent_to_manager', 1)}:email:mgr@ucsh.com",
            ))
            session.add(RequestApprovalState(
                list_id=settings.SP_LIST_LEAVE_REQUESTS, item_id=str(imported.item_id),
                current_version=1, current_snapshot={},
            ))
            await session.commit()
    run(seed())

    entries = [
        ("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": asked.item_id}),
        ("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": not_asked.item_id}),
        ("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": imported.item_id}),
    ]
    result = run(fetch_manager_asked_records(entries))
    assert (settings.SP_LIST_LEAVE_REQUESTS, str(asked.item_id)) in result
    assert (settings.SP_LIST_LEAVE_REQUESTS, str(imported.item_id)) in result
    assert (settings.SP_LIST_LEAVE_REQUESTS, str(not_asked.item_id)) not in result


def test_fetch_manager_asked_records_ignores_a_superseded_version():
    from app.services.employee_validation import fetch_manager_asked_records

    edited = run(_leave(status="pending", approval_version=2))   # re-asked at v2, old v1 row only

    async def seed():
        async with async_session() as session:
            session.add(Outbox(
                request_id=edited.id, message_type="manager_approval", channel="email",
                recipient="mgr@ucsh.com", body="b", status="sent", attempts=1,
                idempotency_key=f"{step_key('leave', edited.item_id, 'sent_to_manager', 1)}:email:mgr@ucsh.com",
            ))
            await session.commit()
    run(seed())

    result = run(fetch_manager_asked_records(
        [("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": edited.item_id})]
    ))
    assert result == set()                                   # the v1 row does not count at v2


def test_fetch_manager_asked_records_ignores_the_state_row_for_an_app_made_request():
    from app.services.employee_validation import fetch_manager_asked_records
    # App-made (sp_item_id None), given a state row by an edit (_sync_version_state)
    # but no manager_approval outbox row: the old table must NOT count it as asked,
    # only an imported request trusts that table (finding 8).
    app_made = run(_leave(status="pending"))

    async def seed():
        async with async_session() as session:
            session.add(RequestApprovalState(
                list_id=settings.SP_LIST_LEAVE_REQUESTS, item_id=str(app_made.item_id),
                current_version=1, current_snapshot={},
            ))
            await session.commit()
    run(seed())

    result = run(fetch_manager_asked_records(
        [("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": app_made.item_id})]
    ))
    assert result == set()                                   # a state row alone does not prove an ask


def test_fetch_manager_asked_records_ignores_a_permanently_failed_ask():
    from app.services.employee_validation import fetch_manager_asked_records
    failed = run(_leave(status="pending"))                   # its only ask failed for good
    retrying = run(_leave(status="pending"))                 # its ask failed but is still retrying

    async def seed():
        async with async_session() as session:
            session.add(Outbox(                               # terminal failure: no next attempt
                request_id=failed.id, message_type="manager_approval", channel="sms",
                recipient="+15555550101", body="b", status="failed", attempts=10, next_attempt_at=None,
                idempotency_key=f"{step_key('leave', failed.item_id, 'sent_to_manager', 1)}:sms:+15555550101",
            ))
            session.add(Outbox(                               # still scheduled to retry: not final
                request_id=retrying.id, message_type="manager_approval", channel="email",
                recipient="mgr@ucsh.com", body="b", status="failed", attempts=1,
                next_attempt_at=utcnow() + timedelta(minutes=5),
                idempotency_key=f"{step_key('leave', retrying.item_id, 'sent_to_manager', 1)}:email:mgr@ucsh.com",
            ))
            await session.commit()
    run(seed())

    result = run(fetch_manager_asked_records([
        ("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": failed.item_id}),
        ("leave", settings.SP_LIST_LEAVE_REQUESTS, {"id": retrying.item_id}),
    ]))
    assert (settings.SP_LIST_LEAVE_REQUESTS, str(failed.item_id)) not in result   # every ask failed: stuck
    assert (settings.SP_LIST_LEAVE_REQUESTS, str(retrying.item_id)) in result      # still retrying: counts


# ===== #423: a pending request's stage is derived from its state =====


def _indexed(**kwargs):
    from app.services.request_live_reads import indexed_request_items
    return run(indexed_request_items("leave", **kwargs))


def _stage_for(items, item_id):
    return next(i["fields"]["stage"] for i in items if str(i["id"]) == str(item_id))


def test_a_pending_request_that_was_asked_shows_waiting_for_the_manager():
    record = run(_leave(status="pending", stage="Submitted, receipt sent"))

    async def seed():
        async with async_session() as session:
            session.add(Outbox(
                request_id=record.id, message_type="manager_approval", channel="email",
                recipient="mgr@ucsh.com", body="b", status="sent", attempts=1,
                idempotency_key=f"{step_key('leave', record.item_id, 'sent_to_manager', 1)}:email:mgr@ucsh.com",
            ))
            await session.commit()
    run(seed())

    assert _stage_for(_indexed(), record.item_id) == "Waiting for Morgan Boss"


def test_a_pending_request_whose_only_manager_is_away_shows_away_until():
    today = rsub.today_toronto()
    # The assigned manager (staff id 5) is on an approved full-day leave over today.
    run(_leave(status="approved", employee_id="5", leave_type="Vacation",
               start_date=today - timedelta(days=1), end_date=today + timedelta(days=3)))
    pending = run(_leave(status="pending", manager_id="5", manager_name="Morgan Boss",
                         stage="Submitted, receipt sent"))   # never asked (no outbox row)

    stage = _stage_for(_indexed(), pending.item_id)
    assert stage.startswith("Waiting, Morgan Boss away until ")


def test_a_decided_request_keeps_its_stored_stage():
    record = run(_leave(status="approved", stage="Approved, balance updated"))
    assert _stage_for(_indexed(), record.item_id) == "Approved, balance updated"


# ===== #424: the admin table marks a request whose send finally failed =====


def test_failed_send_flag_is_set_only_with_a_finally_failed_message():
    failed = run(_leave(status="approved"))
    retrying = run(_leave(status="approved"))
    clean = run(_leave(status="approved"))

    async def seed():
        async with async_session() as session:
            session.add(Outbox(                               # terminal failure: no next attempt
                request_id=failed.id, message_type="result", channel="email", recipient="x@ucsh.com",
                body="b", status="failed", attempts=1, next_attempt_at=None,
                idempotency_key="k-failed",
            ))
            session.add(Outbox(                               # still retrying: not final
                request_id=retrying.id, message_type="result", channel="email", recipient="y@ucsh.com",
                body="b", status="failed", attempts=1, next_attempt_at=utcnow() + timedelta(minutes=5),
                idempotency_key="k-retry",
            ))
            await session.commit()
    run(seed())

    items = _indexed(with_send_status=True)
    flags = {str(i["id"]): i["fields"]["failed_send"] for i in items}
    assert flags[str(failed.item_id)] is True
    assert flags[str(retrying.item_id)] is False
    assert flags[str(clean.item_id)] is False


def test_failed_send_flag_is_absent_without_the_send_status_flag():
    record = run(_leave(status="approved"))
    items = _indexed()                                        # the employee/manager views do not ask for it
    fields = next(i["fields"] for i in items if str(i["id"]) == str(record.item_id))
    assert "failed_send" not in fields


# ===== #427: a bereavement / jury-duty leave queues the HR alert once =====


def test_a_bereavement_leave_queues_the_hr_alert_once(monkeypatch):
    from app.services.leave_requests import BEREAVEMENT_ALERT_RECIPIENTS

    record = run(_leave(status="pending", leave_type="Bereavement"))

    async def enqueue_twice():
        async with async_session() as session:
            fresh = await session.get(RequestRecord, record.id)
            fields = rsub.TypedRequestList("leave")._item_shape(fresh)["fields"]
            await rsub._queue_bereavement_alert(session, fresh, _EMPLOYEE, fields, None)
            await rsub._queue_bereavement_alert(session, fresh, _EMPLOYEE, fields, None)  # a rerun queues nothing new
            await session.commit()
    run(enqueue_twice())

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(Outbox).where(
                    Outbox.request_id == record.id, Outbox.message_type == "bereavement_alert"
                )
            )).scalars().all()

    rows = run(read())
    assert sorted(r.recipient for r in rows) == sorted(BEREAVEMENT_ALERT_RECIPIENTS)
    # HR gets the request's one subject too (#520); the body still opens "Jury Duty / Bereavement Alert".
    assert all(r.subject == thread_subject(record) and r.channel == "email" for r in rows)


def test_a_delivered_bereavement_alert_records_no_stage_event(monkeypatch):
    # A delivered HR alert must not write a stage-changing event on the still-
    # pending request; result_sent here finished the status tracker early (finding 3).
    _stub_email(monkeypatch, result=types.SimpleNamespace(email_id="X"))
    record = run(_leave(status="pending", leave_type="Bereavement", stage="Submitted, receipt sent"))
    row_id = run(_enqueue(record, message_type="bereavement_alert", recipient="mandyl@ucsh.com"))

    run(_deliver(row_id))

    row, events = run(_row_and_events(row_id, record.id))
    assert row.status == "sent"                              # the alert itself went out
    assert events == []                                      # but it recorded no timeline event

    async def reread():
        async with async_session() as session:
            return await session.get(RequestRecord, record.id)

    fresh = run(reread())
    assert fresh.stage == "Submitted, receipt sent"          # the pending stage is untouched
    assert fresh.status == "pending"


def test_bereavement_alert_is_queued_for_an_admin_approved_leave_with_no_employee(monkeypatch):
    from app.services.leave_requests import BEREAVEMENT_ALERT_RECIPIENTS
    # An admin-created Approved bereavement leave: notify False, status approved,
    # no resolved employee. The alert must still be queued, before the notify early
    # return, and without an employee, or HR is never told (finding 4).
    async def no_employee(record, fields):
        return None

    async def no_managers(employee):
        return []

    monkeypatch.setattr(rsub, "_resolve_employee", no_employee)
    monkeypatch.setattr(rsub, "get_all_managers_for_employee", no_managers)
    record = run(_leave(status="approved", leave_type="Bereavement", employee_name="Casey Demo"))

    async def do():
        async with async_session() as session:
            recorded = await rsub._do_submit(
                session, "leave", record.item_id, "admin_dashboard", False, "admin",
            )
            await session.commit()
            return recorded

    assert run(do()) is True

    async def read():
        async with async_session() as session:
            return (await session.execute(
                select(Outbox).where(
                    Outbox.request_id == record.id, Outbox.message_type == "bereavement_alert"
                )
            )).scalars().all()

    rows = run(read())
    assert sorted(r.recipient for r in rows) == sorted(BEREAVEMENT_ALERT_RECIPIENTS)


def test_a_vacation_leave_queues_no_hr_alert():
    record = run(_leave(status="pending", leave_type="Vacation"))

    async def enqueue():
        async with async_session() as session:
            fresh = await session.get(RequestRecord, record.id)
            fields = rsub.TypedRequestList("leave")._item_shape(fresh)["fields"]
            await rsub._queue_bereavement_alert(session, fresh, _EMPLOYEE, fields, None)
            await session.commit()
    run(enqueue())

    async def count():
        async with async_session() as session:
            return len((await session.execute(
                select(Outbox).where(Outbox.request_id == record.id)
            )).scalars().all())

    assert run(count()) == 0


def test_send_bereavement_alert_is_a_no_op_on_the_postgres_path(monkeypatch):
    from app.services import leave_requests as lr

    async def active():
        return True

    async def boom(*_a, **_k):
        raise AssertionError("the direct send must not run on the Postgres path")

    # send_bereavement_alert imports submission_active from request_submission at
    # call time, so patch it at the source, and make any item read fail loudly.
    monkeypatch.setattr(rsub, "submission_active", active)
    monkeypatch.setattr(lr.request_store, "get_list_item", boom)
    # Returns without reading the item or sending: the outbox owns the alert now.
    assert run(lr.send_bereavement_alert(123)) is None
