"""The request page follows its own request after it is sent (#364).

A signed-in employee's submission gets back a signed status_token naming the
request. The page polls GET /api/intake/request-status with it, and each step
(sent to the manager, decided, balance applied, result email) turns from
waiting to done. The token is scoped to one request and cannot be forged.
"""

import asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.routes import intake
from app.services import request_journal as rj
from app.services import request_status


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def clean():
    """Empty the typed request tables before and after each test."""

    async def wipe():
        async with async_session() as session:
            # Children before parents, so foreign keys never block a delete.
            for model in (Outbox, BalanceLedger, RequestEvent, RequestChange, RequestRecord, RequestCounter):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


@pytest.fixture
def client():
    """The intake router alone, for the public request-status endpoint."""
    app = FastAPI()
    app.include_router(intake.router, prefix="/api/intake")
    return TestClient(app)


async def _seed(status: str, events: list[tuple], **record_kwargs) -> int:
    """Store one leave request and append the given timeline events.

    Args:
        status: The request's lifecycle status (lower case).
        events: ``(type, channel, actor, detail)`` tuples, in order.
        **record_kwargs: Extra RequestRecord column overrides (e.g. decided_by,
            decided_channel for the decided-step decider tests).

    Returns:
        The request's item_id.
    """
    async with async_session() as session:
        request = RequestRecord(
            kind="leave", item_id=3905, employee_id="7", manager_id="3",
            manager_name="Dave Powell", leave_type="Vacation",
            start_date=date(2026, 11, 2), status=status, **record_kwargs,
        )
        session.add(request)
        await session.flush()                                   # assign id so events can reference it
        for event_type, channel, actor, detail in events:
            # A unique key per type keeps append_event's idempotency happy.
            key = rj.step_key("leave", request.item_id, event_type, 1)
            await rj.append_event(session, request, event_type, channel, actor, detail, key)
        await session.commit()
        return request.item_id


# ----- the signed status token -----

def test_the_token_round_trips_to_the_request_it_names():
    token = request_status.request_status_token("leave", 500)
    assert request_status.request_ref_from_status_token(token) == ("leave", 500)


def test_a_tampered_or_malformed_token_is_refused():
    token = request_status.request_status_token("leave", 500)
    assert request_status.request_ref_from_status_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert request_status.request_ref_from_status_token("leave:501." + token.split(".")[1]) is None   # id changed
    assert request_status.request_ref_from_status_token("carryover_payout:500." + token.split(".")[1]) is None
    assert request_status.request_ref_from_status_token("leave:abc.deadbeef") is None   # not a number
    assert request_status.request_ref_from_status_token("nope") is None                 # no signature


def test_status_token_for_names_the_stored_kind_or_nothing():
    # The url form maps to the stored kind; a shared token is the same either way.
    assert request_status.status_token_for("carryover-payout", "77") == \
        request_status.request_status_token("carryover_payout", 77)
    assert request_status.status_token_for("leave", None) is None       # no id (should not happen on Postgres)
    assert request_status.status_token_for("lunch", "5") is None         # unknown kind


# ----- the progress read and the route -----

def test_a_pending_request_shows_only_sent_to_the_manager(clean, client):
    item_id = run(_seed("pending", [
        ("submitted", "request_page", "Pat Q Staff", {}),
        ("sent_to_manager", "system", None, {"to": "email", "manager": "Dave Powell"}),
    ]))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["status"] == "pending" and body["finished"] is False
    assert body["manager_name"] == "Dave Powell"
    assert body["steps"]["sent_to_manager"]["done"] is True and body["steps"]["sent_to_manager"]["at"]
    assert body["steps"]["decided"]["done"] is False and body["steps"]["decided"]["result"] is None
    assert body["steps"]["balance"]["done"] is False
    assert body["steps"]["result_sent"]["done"] is False


def test_an_approved_request_shows_the_balance_applied_and_finishes(clean, client):
    item_id = run(_seed("approved", [
        ("submitted", "request_page", "Pat Q Staff", {}),
        ("sent_to_manager", "system", None, {"to": "email", "manager": "Dave Powell"}),
        ("approved", "email_link", "Dave Powell", {}),
        ("balance_applied", "email_link", "Dave Powell", {}),
        ("result_sent", "system", None, {"channel": "email", "to": "pat@ucsh.com"}),
    ]))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["status"] == "approved" and body["finished"] is True
    assert body["steps"]["decided"]["done"] is True and body["steps"]["decided"]["result"] == "approved"
    assert body["steps"]["balance"]["done"] is True and body["steps"]["balance"]["effect"] == "applied"
    assert body["steps"]["result_sent"]["done"] is True and body["steps"]["result_sent"]["at"]


def test_a_rejected_request_moves_no_balance_and_finishes(clean, client):
    item_id = run(_seed("rejected", [
        ("submitted", "request_page", "Pat Q Staff", {}),
        ("sent_to_manager", "system", None, {"to": "email", "manager": "Dave Powell"}),
        ("rejected", "email_link", "Dave Powell", {"reason": "Too short notice"}),
        ("result_sent", "system", None, {"channel": "email", "to": "pat@ucsh.com"}),
    ]))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["status"] == "rejected" and body["finished"] is True
    assert body["steps"]["decided"]["result"] == "rejected"
    assert body["steps"]["balance"]["done"] is False and body["steps"]["balance"]["effect"] is None


def test_a_forged_token_is_refused_by_the_route(clean, client):
    response = client.get("/api/intake/request-status", params={"token": "leave:3905.deadbeef"})
    assert response.status_code == 401


def test_a_valid_token_for_a_missing_request_is_not_found(clean, client):
    token = request_status.request_status_token("leave", 999999)   # well-signed, but no such row
    assert client.get("/api/intake/request-status", params={"token": token}).status_code == 404


# ----- #371: the decided step names who actually decided -----

def test_the_decided_step_names_an_admin_when_an_admin_decided(clean, client):
    # An admin decision (actor "admin", channel admin_dashboard) reads as "an admin".
    item_id = run(_seed("rejected", [
        ("submitted", "request_page", "Pat Q Staff", {}),
        ("sent_to_manager", "system", None, {"manager": "Dave Powell"}),
        ("rejected", "admin_dashboard", "admin", {"reason": "duplicate"}),
        ("result_sent", "system", None, {}),
    ], decided_by="admin", decided_channel="admin_dashboard"))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["steps"]["decided"]["done"] is True
    assert body["steps"]["decided"]["decider"] == "an admin"


def test_the_decided_step_names_the_manager_when_the_manager_decided(clean, client):
    # The assigned manager (staff id "3", the seeded manager_id) decided: their name.
    item_id = run(_seed("approved", [
        ("submitted", "request_page", "Pat Q Staff", {}),
        ("sent_to_manager", "system", None, {"manager": "Dave Powell"}),
        ("approved", "manager_dashboard", "3", {}),
        ("result_sent", "system", None, {}),
    ], decided_by="3", decided_channel="manager_dashboard"))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["steps"]["decided"]["decider"] == "Dave Powell"


def test_a_request_never_sent_to_a_manager_is_not_shown_as_sent(clean, client):
    # Every manager was off at submit, so no sent_to_manager event exists: the step
    # stays not-done (the page then says it is waiting for a manager) (#370).
    item_id = run(_seed("pending", [
        ("submitted", "request_page", "Pat Q Staff", {}),
    ]))
    token = request_status.request_status_token("leave", item_id)
    body = client.get("/api/intake/request-status", params={"token": token}).json()

    assert body["steps"]["sent_to_manager"]["done"] is False
    assert body["steps"]["decided"]["done"] is False
    assert body["steps"]["decided"]["decider"] is None
