"""The request lists in Postgres behave like the SharePoint lists (#189, #190).

Every call the app makes against a request list goes through request_store.
With STORAGE_REQUESTS at "sharepoint" it must reach sp_client untouched; at
"postgres" it must reach the matching table and answer in Graph's shapes, so
the request services cannot tell the difference.
"""

import asyncio

import httpx
import pytest
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.models.request_item import CarryoverPayoutRequestItem, LeaveRequestItem, OvertimeRequestItem
from app.repositories import request_store as store_module
from app.repositories.request_store import request_store

LEAVE = settings.SP_LIST_LEAVE_REQUESTS


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def postgres(monkeypatch):
    """Requests in Postgres, starting from empty tables."""
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")

    async def wipe():
        async with async_session() as session:
            for model in (LeaveRequestItem, OvertimeRequestItem, CarryoverPayoutRequestItem):
                await session.execute(delete(model))
            await session.commit()

    run(wipe())
    yield
    run(wipe())


def test_a_created_item_comes_back_in_the_graph_shape(postgres):
    item = run(request_store.create_list_item(LEAVE, {"LeaveType": "Vacation", "Status": "Pending"}))
    assert isinstance(item["id"], str)                          # Graph sends ids as strings
    assert item["fields"]["id"] == item["id"]
    assert item["fields"]["LeaveType"] == "Vacation"
    assert item["fields"]["Created"].endswith("Z")
    assert item["createdDateTime"] == item["fields"]["Created"]


def test_an_update_merges_fields_and_keeps_the_rest(postgres):
    item = run(request_store.create_list_item(LEAVE, {"Status": "Pending", "Days": 2.0}))
    fields = run(request_store.update_list_item_fields(LEAVE, item["id"], {"Status": "Approved"}))
    assert fields["Status"] == "Approved"
    assert fields["Days"] == 2.0                                # untouched field kept, like a PATCH
    again = run(request_store.get_list_item(LEAVE, int(item["id"])))   # int ids work too
    assert again["fields"]["Status"] == "Approved"


def test_a_missing_item_raises_the_same_404_as_graph(postgres):
    with pytest.raises(httpx.HTTPStatusError) as err:
        run(request_store.get_list_item(LEAVE, "999"))
    assert err.value.response.status_code == 404
    assert run(request_store.get_list_item_or_none(LEAVE, "999")) is None
    with pytest.raises(httpx.HTTPStatusError):
        run(request_store.update_list_item_fields(LEAVE, "999", {"Status": "Approved"}))


def test_system_fields_are_not_stored_from_a_caller(postgres):
    item = run(request_store.create_list_item(LEAVE, {"id": "5", "Created": "x", "Status": "Pending"}))
    assert item["fields"]["Created"] != "x"                     # SharePoint fills these in itself


def test_the_lists_are_kept_apart(postgres):
    run(request_store.create_list_item(LEAVE, {"Status": "Pending"}))
    run(request_store.create_list_item(settings.SP_LIST_OVERTIME_REQUESTS, {"Hours": 4}))
    assert len(run(request_store.get_list_items(LEAVE))) == 1
    assert len(run(request_store.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT))) == 0


def test_select_keeps_only_the_named_fields(postgres):
    run(request_store.create_list_item(LEAVE, {"Status": "Pending", "Days": 1}))
    [item] = run(request_store.get_list_items(LEAVE, select=["Status"]))
    assert set(item["fields"]) == {"id", "Status"}


def test_a_copied_item_keeps_its_id_and_source(postgres):
    table = store_module.request_list_tables()[LEAVE]
    run(table.create_list_item(LEAVE, {"Status": "Pending"}, item_id=412, sp_item_id="412"))
    assert run(request_store.get_list_item(LEAVE, "412"))["id"] == "412"
    assert run(table.find_by_sp_item_id("412"))["id"] == "412"
    newer = run(request_store.create_list_item(LEAVE, {"Status": "Pending"}))
    assert int(newer["id"]) > 412                               # new requests never reuse a copied id


def test_delete_removes_the_item(postgres):
    item = run(request_store.create_list_item(LEAVE, {"Status": "Pending"}))
    run(request_store.delete_list_item(LEAVE, item["id"]))
    assert run(request_store.get_list_item_or_none(LEAVE, item["id"])) is None


def test_sharepoint_is_used_until_the_setting_changes(monkeypatch):
    calls = []

    async def fake_get(list_id, item_id):
        calls.append((list_id, item_id))
        return {"id": str(item_id), "fields": {}}

    monkeypatch.setattr(store_module.sp_client, "get_list_item", fake_get)
    run(request_store.get_list_item(LEAVE, "7"))                # default: sharepoint
    assert calls == [(LEAVE, "7")]


def test_other_lists_always_go_to_sharepoint(monkeypatch, postgres):
    calls = []

    async def fake_items(list_id, **kwargs):
        calls.append(list_id)
        return []

    monkeypatch.setattr(store_module.sp_client, "get_list_items", fake_items)
    run(request_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY))
    assert calls == [settings.SP_LIST_STAFF_DIRECTORY]


def test_a_mistyped_setting_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgress")
    with pytest.raises(NotImplementedError):
        run(request_store.get_list_items(LEAVE))
