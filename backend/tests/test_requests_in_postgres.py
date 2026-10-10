"""A leave request kept in Postgres goes through approval like a SharePoint one (#193).

With STORAGE_REQUESTS at "postgres" the approval service reads the item, runs
the conflict check over the other leave requests, claims the action and writes
the outcome, all against the Postgres table. Only the Staff Directory and email
are faked; the request itself never touches SharePoint.
"""

import asyncio
import random

import pytest
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.request_item import LeaveRequestItem
from app.repositories.request_store import request_list_tables, request_store
from app.services import leave_requests

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
PAT = {"id": "7", "fields": {"Title": "Pat Staff", "EmailAddress": "pat@ucsh.com"}}
BOSS = {"id": "3", "fields": {"Title": "Boss Person", "EmailAddress": "boss@ucsh.com"}}


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def postgres(monkeypatch):
    """Requests in Postgres, no SharePoint calls, emails captured."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")

    async def wipe():
        async with async_session() as session:
            await session.execute(delete(LeaveRequestItem))
            await session.commit()

    async def no_sharepoint(*args, **kwargs):
        raise AssertionError("a request call reached SharePoint")

    for name in ("get_list_item", "get_list_item_or_none", "create_list_item", "update_list_item_fields"):
        monkeypatch.setattr(sharepoint.sp_client, name, no_sharepoint)

    async def submitter(fields, column):
        return PAT

    async def by_id(employee_id):
        return BOSS if str(employee_id) == "3" else PAT

    sent = []

    async def capture(**kwargs):
        sent.append(kwargs)

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(leave_requests, "resolve_request_submitter", submitter)
    monkeypatch.setattr(leave_requests, "get_employee_by_id", by_id)
    monkeypatch.setattr(leave_requests, "send_email_with_dashboard", capture)
    monkeypatch.setattr(leave_requests, "notify_requests_blocked_by_approval", nothing)
    run(wipe())
    yield sent
    run(wipe())


def _pending_leave(leave_type: str = "Bereavement") -> str:
    """Store a pending leave request under a fresh id; return the id."""
    item_id = random.randint(10_000_000, 99_999_999)            # the claim log outlives the table wipe
    table = request_list_tables()[LEAVE]
    run(table.create_list_item(LEAVE, {
        "LeaveType": leave_type, "Status": "Pending", "StartDate": "2026-11-02",
        "EndDate": "2026-11-03", "Days": 2.0, "SubmitterEmail": "pat@ucsh.com",
    }, item_id=item_id))
    return str(item_id)


def test_approving_a_postgres_request_writes_the_outcome_there(postgres):
    request_id = _pending_leave()
    result = run(leave_requests.approve_leave_request(request_id, "3"))
    assert result == {"status": "approved", "no_balance_change": True}
    fields = run(request_store.get_list_item(LEAVE, request_id))["fields"]
    assert fields["Status"] == "Approved"
    assert fields["ApproveProcessedFlag"] == "Processed"
    assert postgres[0]["to"] == ["pat@ucsh.com"]                 # the approval email went out


def test_a_second_approval_is_refused(postgres):
    request_id = _pending_leave()
    run(leave_requests.approve_leave_request(request_id, "3"))
    assert run(leave_requests.approve_leave_request(request_id, "3")) == {"error": "Already processed"}


def test_a_deleted_postgres_request_is_reported_not_raised(postgres):
    assert run(leave_requests.approve_leave_request("12345678", "3")) == {"error": "This request no longer exists."}
