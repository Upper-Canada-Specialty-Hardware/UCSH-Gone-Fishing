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


# The Microsoft Form fallback (#195): once requests live in Postgres, a new item
# the Form writes to a SharePoint list is moved into Postgres and processed there.

FORM_ITEM = {
    "id": "950", "createdDateTime": "2026-10-10T13:00:00Z",
    "fields": {"LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02",
               "EndDate": "2026-11-02", "SubmittedTestLookupId": "21"},
}


@pytest.fixture
def form_item(sharepoint_lists, monkeypatch):
    """Requests in Postgres; SharePoint holds one new Form item (or none, once deleted)."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    inbox = {"950": FORM_ITEM}

    async def fake_item(list_id, item_id):
        return inbox.get(str(item_id))

    monkeypatch.setattr(request_copy.sp_client, "get_list_item_or_none", fake_item)
    return inbox


def test_a_form_item_is_moved_once_with_a_new_id(form_item):
    moved = run(request_copy.move_form_item_into_postgres(LEAVE, "950"))
    assert moved["fields"]["LeaveType"] == "Vacation"
    assert moved["fields"]["SubmitterEmail"] == "pat@ucsh.com"
    assert run(request_list_tables()[LEAVE].find_by_sp_item_id("950"))["id"] == moved["id"]
    assert run(request_copy.move_form_item_into_postgres(LEAVE, "950")) is None   # never twice


def test_a_deleted_form_item_is_skipped(form_item):
    form_item.clear()
    assert run(request_copy.move_form_item_into_postgres(LEAVE, "950")) is None


def test_the_dispatcher_processes_the_postgres_copy(form_item, monkeypatch):
    from app.services import leave_requests
    from app.tasks import dispatcher
    handled = []

    async def record(item_id):
        handled.append(str(item_id))

    for name in ("auto_calculate_days", "auto_assign_manager", "send_bereavement_alert"):
        monkeypatch.setattr(leave_requests, name, record)
    run(dispatcher.dispatch_change(LEAVE, {"id": "950", "fields": {}}))
    moved = run(request_list_tables()[LEAVE].find_by_sp_item_id("950"))
    assert handled == [moved["id"]] * 3                         # processed under its Postgres id
    run(dispatcher.dispatch_change(LEAVE, {"id": "950", "fields": {}}))
    assert len(handled) == 3                                    # a second notice changes nothing
