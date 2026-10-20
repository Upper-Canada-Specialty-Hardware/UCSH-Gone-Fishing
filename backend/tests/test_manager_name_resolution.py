"""Resolving a manager id to a name follows the backend staff live in (finding 7).

Before the switch a manager is a SharePoint user id, resolved against the site's
User Information List. Once staff live in Postgres there is no such list: a
manager is their own staff id and the name comes from the staff table. These pin
that the Postgres path reads the staff table and never falls back to the
SharePoint user list (#265).
"""

import asyncio

from app.services import manager_assignments


def _run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


def test_manager_id_to_name_reads_the_staff_table_on_postgres(monkeypatch):
    # Postgres: the map is {staff id: name} from the staff store, and the
    # SharePoint user-list helper must not be called at all.
    async def in_postgres():
        return True

    async def fake_items(*_a, **_k):
        return [
            {"id": "77", "fields": {"Title": "Grace Hopper"}},
            {"id": "80", "fields": {"Title": "Ada Lovelace"}},
        ]

    async def boom():
        raise AssertionError("the SharePoint user list must not be read on Postgres")

    monkeypatch.setattr(manager_assignments, "staff_in_postgres", in_postgres)
    monkeypatch.setattr(manager_assignments.staff_store, "get_list_items", fake_items)
    monkeypatch.setattr(manager_assignments, "_get_sp_user_name_map", boom)

    result = _run(manager_assignments._manager_id_to_name())

    assert result == {77: "Grace Hopper", 80: "Ada Lovelace"}


def test_manager_id_to_name_uses_the_user_list_on_sharepoint(monkeypatch):
    # SharePoint: the map still comes from the User Information List, unchanged.
    async def in_postgres():
        return False

    async def fake_user_map():
        return {77: "Grace Hopper"}

    monkeypatch.setattr(manager_assignments, "staff_in_postgres", in_postgres)
    monkeypatch.setattr(manager_assignments, "_get_sp_user_name_map", fake_user_map)

    result = _run(manager_assignments._manager_id_to_name())

    assert result == {77: "Grace Hopper"}
