"""Copying the request lists from SharePoint into Postgres (#194).

The copy keeps each item's SharePoint id and times, fills SubmitterEmail from
the person column, can run again while SharePoint is still the original, and
refuses once requests live in Postgres.
"""

import asyncio

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_item import CarryoverPayoutRequestItem, LeaveRequestItem, OvertimeRequestItem
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.repositories.request_store import request_list_tables
from app.services.request_copy import _json_copy_tables
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
        # The bulk copy writes the JSON tables; the Form fallback writes the live
        # typed requests table. Clear both (typed change rows and counters too).
        async with async_session() as session:
            for model in (LeaveRequestItem, OvertimeRequestItem, CarryoverPayoutRequestItem,
                          RequestChange, RequestRecord, RequestCounter, LegacyFinding, SharepointSeen):
                await session.execute(delete(model))
            await session.commit()

    monkeypatch.setattr(request_copy.sp_client, "get_list_items", fake_items)
    run(wipe())
    yield lists
    run(wipe())


def test_the_copy_keeps_ids_times_and_fills_the_email(sharepoint_lists):
    report = run(request_copy.copy_requests_from_sharepoint())
    assert report["lists"]["leave"] == {"source_count": 1, "created": 1, "updated": 0}
    leave = run(_json_copy_tables()[LEAVE].get_list_item(LEAVE, "412"))
    assert leave["createdDateTime"] == "2026-03-01T15:00:00Z"
    assert leave["fields"]["SubmitterEmail"] == "pat@ucsh.com"     # from the person column
    assert "@odata.etag" not in leave["fields"]
    overtime = run(_json_copy_tables()[OVERTIME].get_list_item(OVERTIME, "88"))
    assert overtime["fields"]["SubmitterEmail"] == "lee@gmail.com"  # an existing email is kept


def test_running_again_overwrites_with_sharepoints_values(sharepoint_lists):
    run(request_copy.copy_requests_from_sharepoint())
    sharepoint_lists[LEAVE][0]["fields"]["Status"] = "Cancelled"
    report = run(request_copy.copy_requests_from_sharepoint())
    assert report["lists"]["leave"] == {"source_count": 1, "created": 0, "updated": 1}
    leave = run(_json_copy_tables()[LEAVE].get_list_item(LEAVE, "412"))
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


# The Microsoft Form is retired at the switch (no fallback, revised #336 scope):
# once requests live in Postgres a new item that still lands in a SharePoint list
# is never imported, only flagged for an admin on the Legacy data screen.

FORM_ITEM = {
    "id": "950", "createdDateTime": "2026-10-10T13:00:00Z",
    "fields": {"LeaveType": "Vacation", "Status": "Pending", "StartDate": "2026-11-02",
               "EndDate": "2026-11-02", "EmployeeID": "21", "SubmittedTestLookupId": "21",
               "SubmitterEmail": "pat@ucsh.com"},
}


@pytest.fixture
def form_item(sharepoint_lists, monkeypatch):
    """Requests in Postgres; SharePoint holds one new item a person or the Form wrote."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    inbox = {"950": FORM_ITEM}

    async def fake_item(list_id, item_id):
        return inbox.get(str(item_id))

    monkeypatch.setattr(request_copy.sp_client, "get_list_item_or_none", fake_item)
    return inbox


async def _findings_for(sp_item_id, case):
    """Legacy findings of one case for a SharePoint item id."""
    async with async_session() as session:
        return (await session.execute(
            select(LegacyFinding).where(
                LegacyFinding.subject_id == str(sp_item_id), LegacyFinding.case == case
            )
        )).scalars().all()


def test_the_dispatcher_flags_a_new_item_and_never_imports_it(form_item):
    # Since the Form is retired, a request-list webhook once requests live in
    # Postgres runs the per-item SharePoint check, which records one legacy finding
    # for the new item and imports nothing (services/sharepoint_check).
    from app.tasks import dispatcher
    run(dispatcher.dispatch_change(LEAVE, {"id": "950", "fields": {}}))
    assert run(request_list_tables()[LEAVE].find_by_sp_item_id("950")) is None   # never imported
    findings = run(_findings_for("950", "sharepoint_new_after_switch"))
    assert len(findings) == 1                                                    # flagged for an admin
    assert findings[0].evidence.get("submitter_email") == "pat@ucsh.com"         # id, list, submitter, dates
