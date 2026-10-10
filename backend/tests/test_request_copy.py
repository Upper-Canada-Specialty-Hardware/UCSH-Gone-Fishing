"""Copying the request lists from SharePoint into Postgres (#194).

The copy keeps each item's SharePoint id and times, fills SubmitterEmail from
the person column, can run again while SharePoint is still the original, and
refuses once requests live in Postgres.
"""

import asyncio

import pytest
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.models.request_item import CarryoverPayoutRequestItem, LeaveRequestItem, OvertimeRequestItem
from app.repositories.request_store import request_list_tables
from app.services import request_copy

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
OVERTIME = settings.SP_LIST_OVERTIME_REQUESTS

USERS = [{"id": "21", "fields": {"EMail": "Pat@UCSH.com"}}]


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def sharepoint_lists(monkeypatch):
    """Fake SharePoint lists; empty Postgres tables before and after."""
    lists = {
        LEAVE: [{
            "id": "412", "createdDateTime": "2026-03-01T15:00:00Z", "lastModifiedDateTime": "2026-03-02T15:00:00Z",
            "fields": {"@odata.etag": "x", "id": "412", "LeaveType": "Vacation", "Status": "Approved",
                       "SubmittedTestLookupId": "21"},
        }],
        OVERTIME: [{
            "id": "88", "createdDateTime": "2026-04-01T15:00:00Z",
            "fields": {"Hours": 4.0, "Status": "Pending", "SubmitterEmail": "lee@gmail.com"},
        }],
        settings.SP_LIST_CARRYOVER_PAYOUT: [],
        "User Information List": USERS,
    }

    async def fake_items(list_id, **kwargs):
        return lists[list_id]

    async def wipe():
        async with async_session() as session:
            for model in (LeaveRequestItem, OvertimeRequestItem, CarryoverPayoutRequestItem):
                await session.execute(delete(model))
            await session.commit()

    monkeypatch.setattr(request_copy.sp_client, "get_list_items", fake_items)
    run(wipe())
    yield lists
    run(wipe())


def test_the_copy_keeps_ids_times_and_fills_the_email(sharepoint_lists):
    report = run(request_copy.copy_requests_from_sharepoint())
    assert report["lists"]["leave"] == {"source_count": 1, "created": 1, "updated": 0}
    leave = run(request_list_tables()[LEAVE].get_list_item(LEAVE, "412"))
    assert leave["createdDateTime"] == "2026-03-01T15:00:00Z"
    assert leave["fields"]["SubmitterEmail"] == "pat@ucsh.com"     # from the person column
    assert "@odata.etag" not in leave["fields"]
    overtime = run(request_list_tables()[OVERTIME].get_list_item(OVERTIME, "88"))
    assert overtime["fields"]["SubmitterEmail"] == "lee@gmail.com"  # an existing email is kept


def test_running_again_overwrites_with_sharepoints_values(sharepoint_lists):
    run(request_copy.copy_requests_from_sharepoint())
    sharepoint_lists[LEAVE][0]["fields"]["Status"] = "Cancelled"
    report = run(request_copy.copy_requests_from_sharepoint())
    assert report["lists"]["leave"] == {"source_count": 1, "created": 0, "updated": 1}
    leave = run(request_list_tables()[LEAVE].get_list_item(LEAVE, "412"))
    assert leave["fields"]["Status"] == "Cancelled"


def test_the_copy_is_refused_once_requests_live_in_postgres(sharepoint_lists, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    with pytest.raises(request_copy.RequestsAlreadyMoved):
        run(request_copy.copy_requests_from_sharepoint())


def test_the_report_counts_what_is_left_to_copy(sharepoint_lists):
    before = run(request_copy.request_storage_report())
    assert before["storage"] == "sharepoint"
    assert before["lists"]["leave"] == {"postgres_count": 0, "sharepoint_count": 1, "only_in_sharepoint": 1}
    run(request_copy.copy_requests_from_sharepoint())
    after = run(request_copy.request_storage_report())
    assert after["lists"]["leave"]["only_in_sharepoint"] == 0
