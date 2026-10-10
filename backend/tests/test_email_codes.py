"""Emailed sign-in codes for the public request page (#131).

A person proves they own an address by typing back a code sent to it. What is
covered: the code works once, for 10 minutes, for 5 tries; sending is limited
per address and per caller; the answer to "send me a code" never reveals who
is on staff; and a verified employee gets the same signed dashboard token the
emailed links carry, while anyone else gets a separate verified-email token.
"""

import asyncio
import itertools
import time
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.database import async_session
from app.models import EmailCode
from app.models.mixins import utcnow
from app.routes import intake
from app.services import email_codes
from app.services.dashboard_tokens import validate_dashboard_token

_counter = itertools.count()


def _unique(prefix: str) -> str:
    """A fresh address per test, so rows from other tests never interfere."""
    return f"{prefix}{next(_counter)}-{time.time_ns()}@example.com"


def _ip() -> str:
    """A caller id no earlier run has used, so the per-IP limit starts at zero.

    The test database keeps rows between runs, so anything short of unique
    (a random dotted IP) can collide with a row still inside the window.
    """
    return f"test-{uuid.uuid4().hex}"


@pytest.fixture
def client(monkeypatch):
    """The intake router alone, with email sending and the directory stubbed.

    Returns a TestClient with ``.sent`` (emails "sent") and ``.staff`` (the
    stand-in Staff Directory, keyed by lowercase email) attached.
    """
    app = FastAPI()
    app.include_router(intake.router, prefix="/api/intake")
    test_client = TestClient(app)
    test_client.sent = []
    test_client.staff = {}

    async def fake_send_email(to, subject, html_body, **kwargs):
        test_client.sent.append({"to": to, "subject": subject, "html": html_body})

    async def fake_get_employee_by_email(email):
        return test_client.staff.get(email.strip().lower())

    monkeypatch.setattr(intake, "send_email", fake_send_email)
    monkeypatch.setattr(intake, "get_employee_by_email", fake_get_employee_by_email)
    return test_client


@pytest.fixture
def fixed_code(monkeypatch):
    """Make every issued code "123456" so tests can type it back."""
    monkeypatch.setattr(email_codes, "generate_code", lambda: "123456")
    return "123456"


def _ask(client, email, ip=None):
    """POST /code from a given caller IP."""
    return client.post(
        "/api/intake/code", json={"email": email},
        headers={"x-forwarded-for": ip or _ip()},
    )


def _verify(client, email, code):
    """POST /verify."""
    return client.post("/api/intake/verify", json={"email": email, "code": code})


# ----- pure helpers -----

def test_codes_are_six_digits_and_keep_leading_zeros():
    for _ in range(200):
        code = email_codes.generate_code()
        assert len(code) == 6 and code.isdigit()


def test_a_hash_is_bound_to_its_address():
    assert email_codes.hash_code("a@x.com", "123456") != email_codes.hash_code("b@x.com", "123456")


def test_verified_email_token_round_trips_and_rejects_tampering():
    signed = email_codes.sign_verified_email("new@x.com")
    assert email_codes.check_verified_email("new@x.com", signed["exp"], signed["token"])
    assert not email_codes.check_verified_email("other@x.com", signed["exp"], signed["token"])
    assert not email_codes.check_verified_email("new@x.com", str(int(signed["exp"]) + 1), signed["token"])


def test_verified_email_token_expires():
    signed = email_codes.sign_verified_email("new@x.com", expiry=int(time.time()) - 1)
    assert not email_codes.check_verified_email("new@x.com", signed["exp"], signed["token"])


def test_a_code_expires_after_ten_minutes():
    now = utcnow()
    assert not email_codes.is_expired(now - timedelta(minutes=9), now)
    assert email_codes.is_expired(now - timedelta(minutes=11), now)


# ----- sending -----

def test_known_and_unknown_addresses_get_the_same_answer_and_both_get_a_code(client):
    known, unknown = _unique("known"), _unique("unknown")
    client.staff[known] = {"id": "7", "fields": {"Title": "Pat Staff"}}

    a, b = _ask(client, known), _ask(client, unknown)

    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert [m["to"] for m in client.sent] == [[known], [unknown]]


def test_an_address_gets_at_most_three_codes_per_window_and_the_answer_does_not_change(client):
    email = _unique("flood")

    answers = [_ask(client, email).json() for _ in range(4)]

    assert len(client.sent) == 3
    assert answers[3] == answers[0]


def test_one_caller_can_request_at_most_ten_codes_per_window(client):
    ip = _ip()

    for _ in range(11):
        _ask(client, _unique("spray"), ip=ip)

    assert len(client.sent) == 10


def test_an_address_that_is_not_an_email_is_refused(client):
    assert _ask(client, "not-an-email").status_code == 400
    assert client.sent == []


def test_a_failed_send_is_reported(client, monkeypatch):
    async def broken_send(**kwargs):
        raise RuntimeError("email service down")

    monkeypatch.setattr(intake, "send_email", broken_send)
    assert _ask(client, _unique("down")).status_code == 502


# ----- checking -----

def test_a_known_employee_gets_a_valid_thirty_day_dashboard_token(client, fixed_code):
    email = _unique("known")
    client.staff[email] = {"id": "42", "fields": {"Title": "Pat Staff"}}
    _ask(client, email)

    body = _verify(client, email, fixed_code).json()

    assert body["status"] == "employee" and body["name"] == "Pat Staff"
    assert body["role"] == "employee" and body["uid"] == "42"
    assert validate_dashboard_token("employee", "42", body["token"], body["exp"]) == (True, "")
    assert int(body["exp"]) - time.time() > 29 * 86400


def test_an_unknown_address_gets_a_verified_email_token(client, fixed_code):
    email = _unique("newhire")
    _ask(client, email)

    body = _verify(client, email, fixed_code).json()

    assert body["status"] == "unknown"
    v = body["verified"]
    assert v["email"] == email
    assert email_codes.check_verified_email(email, v["exp"], v["token"])


def test_the_address_is_matched_whatever_its_case(client, fixed_code):
    email = _unique("case")
    _ask(client, email.upper())

    assert _verify(client, f"  {email.upper()} ", fixed_code).status_code == 200


def test_a_code_works_only_once(client, fixed_code):
    email = _unique("once")
    _ask(client, email)

    assert _verify(client, email, fixed_code).status_code == 200
    assert _verify(client, email, fixed_code).status_code == 400


def test_a_wrong_code_says_how_many_tries_are_left(client, fixed_code):
    email = _unique("wrong")
    _ask(client, email)

    response = _verify(client, email, "000000")

    assert response.status_code == 400
    assert "4 tries left" in response.json()["detail"]


def test_five_wrong_tries_lock_the_code_even_against_the_right_one(client, fixed_code):
    email = _unique("lock")
    _ask(client, email)

    for _ in range(5):
        _verify(client, email, "000000")

    response = _verify(client, email, fixed_code)
    assert response.status_code == 400
    assert "Ask for a new code" in response.json()["detail"]


async def _attempts_of(email):
    """The newest code's try count for an address."""
    async with async_session() as session:
        row = await session.scalar(
            select(EmailCode).where(EmailCode.email == email)
            .order_by(EmailCode.created_at.desc(), EmailCode.id.desc()).limit(1)
        )
        return row.attempts


def test_parallel_guesses_cannot_get_past_the_limit(client, fixed_code):
    # Twenty wrong guesses sent at once: each try slot is taken in the database,
    # so exactly five are counted and the right code is then locked out too.
    email = _unique("burst")
    _ask(client, email)

    async def burst():
        return await asyncio.gather(*(email_codes.check_code(email, "000000") for _ in range(20)))

    results = asyncio.run(burst())

    assert not any(r.ok for r in results)
    assert asyncio.run(_attempts_of(email)) == email_codes.MAX_ATTEMPTS
    assert not asyncio.run(email_codes.check_code(email, fixed_code)).ok


def test_two_parallel_right_answers_use_the_code_once(client, fixed_code):
    email = _unique("twice")
    _ask(client, email)

    async def both():
        return await asyncio.gather(*(email_codes.check_code(email, fixed_code) for _ in range(2)))

    assert sum(r.ok for r in asyncio.run(both())) == 1


def test_asking_again_retires_the_older_code(client, monkeypatch):
    email = _unique("again")
    codes = iter(["111111", "222222"])
    monkeypatch.setattr(email_codes, "generate_code", lambda: next(codes))
    _ask(client, email)
    _ask(client, email)

    assert _verify(client, email, "111111").status_code == 400
    assert _verify(client, email, "222222").status_code == 200


def test_an_expired_code_is_refused(client, fixed_code):
    email = _unique("old")
    _ask(client, email)

    async def age_the_code():
        async with async_session() as session:
            await session.execute(
                update(EmailCode).where(EmailCode.email == email)
                .values(created_at=utcnow() - timedelta(minutes=11))
            )
            await session.commit()

    asyncio.run(age_the_code())

    response = _verify(client, email, fixed_code)
    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


def test_no_code_requested_is_refused(client):
    response = _verify(client, _unique("never"), "123456")
    assert response.status_code == 400
