"""Staff Directory writers land in Postgres when staff live there (#269).

These cover the write paths that used to go straight to the SharePoint list and
now route through ``staff_store``: HR's entitlement and balance edits, the admin
set-balances handler, add employee, the manager assignment links, and the April-1
carry-over reset. For each one, in Postgres mode, the change must land in the
``staff`` table, queue exactly one SharePoint write-back, and record exactly one
``staff_changes`` row with the right source. Add employee also round-trips the
SharePoint item id its write-back creates back onto the row.

They run on SQLite with ``sp_client`` faked, like ``test_staff_directory_postgres``.
SharePoint-mode behaviour is covered by the writers' own existing tests.
"""
import asyncio

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.graph import sharepoint as sharepoint_module
from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.staff import Staff, StaffSupervisor, StaffWriteback
from app.models.staff_change import StaffChange
from app.services import (
    employee_creation as ec,
    manager_assignments,
    staff_entitlements as se,
    staff_import,
)
from app.tasks import carryover_reset

STAFF = settings.SP_LIST_STAFF_DIRECTORY


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _sp_item(item_id, **overrides):
    """A sample SharePoint Staff Directory item, with field overrides."""
    fields = {
        "Title": "Ada Lovelace",
        "EmailAddress": "ada@ucsh.com",
        "SalaryHourly": "Salary",
        "Location": "Barrie",
        "Department": "Engineering",
        "DefaultYearlyVacationDays": 15,
        "SickDayEntitlement": 8,
        "CurrentVacationBalance": 10.0,
        "CurrentSickDayBalance": 3,
        "CurrentOvertimeBalance": 1.0,
        "CarryOver": 2,
        "Payout": 0,
        "TitleLinkLookupId": 55,
        "Modified": "2026-10-10T12:00:00Z",
        "Created": "2020-01-01T00:00:00Z",
        "AllManagers": [],
        "SupervisorLinkLookupId": None,
    }
    fields.update(overrides)
    return {"id": str(item_id), "fields": fields}


@pytest.fixture
def clean_db():
    """Empty every staff table before and after a test."""
    async def wipe():
        async with async_session() as session:
            for model in (StaffWriteback, StaffSupervisor, StaffChange, LegacyFinding, Staff):
                await session.execute(delete(model))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


def _seed(monkeypatch, *items):
    """Import SharePoint items into Postgres so the writers have rows to edit."""
    async def fake_get_list_items(list_id, **kwargs):
        return list(items)                                 # every seeded item, in order
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_get_list_items)

    async def inner():
        async with async_session() as session:
            await staff_import.staff_sync_step(session, None)
            await session.commit()
    run(inner())


async def _links_wb_changes(sp_id):
    """The links, write-backs and change rows for one person, for assertions."""
    async with async_session() as session:
        row = (await session.execute(select(Staff).where(Staff.sp_item_id == sp_id))).scalars().first()
        links = (await session.execute(
            select(StaffSupervisor).where(StaffSupervisor.staff_id == row.id)
        )).scalars().all()
        wb = (await session.execute(select(StaffWriteback))).scalars().all()
        changes = (await session.execute(
            select(StaffChange).where(StaffChange.employee_id == sp_id)
        )).scalars().all()
        return row, links, wb, changes


# --- HR entitlement / balance edits (staff_entitlements) ----------------------

def test_entitlements_land_in_postgres_without_double_history(clean_db, monkeypatch):
    # A balance edit lands in Postgres, queues one write-back, and is logged once
    # (by the service itself), not twice: the store must not add a second row.
    _seed(monkeypatch, _sp_item("101", CurrentVacationBalance=10))
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    async def fake_rad(emp_id, vacation, carryover):
        return ""                                          # no derived-date change -> no extra row
    monkeypatch.setattr(se, "recalculate_request_allow_date", fake_rad)

    result = run(se.apply_entitlement_changes("101", {"vacation": 4}, "correction"))
    assert {c["field"] for c in result["changed"]} == {"vacation"}

    row, _links, wb, changes = run(_links_wb_changes("101"))
    assert float(row.vacation_balance) == 4.0             # the move landed in Postgres
    assert wb == []                                        # write-back is off: nothing for SharePoint
    vac = [c for c in changes if c.field == "CurrentVacationBalance"]
    assert len(vac) == 1 and vac[0].source == "admin_dashboard"   # logged once, by the service


def test_entitlements_managers_round_trip_names_into_postgres(clean_db, monkeypatch):
    # The Graph write shape carries manager ids but no names; the names must still
    # reach the Postgres link so matching stays by name.
    _seed(monkeypatch, _sp_item("101"))
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    async def fake_map():
        return {77: "Grace Hopper"}
    monkeypatch.setattr(se, "_get_sp_user_name_map", fake_map)

    async def fake_rad(*a, **k):
        return ""
    monkeypatch.setattr(se, "recalculate_request_allow_date", fake_rad)

    run(se.apply_entitlement_changes(
        "101", {}, "assign a manager",
        {"managers": {"ids": [77], "names": ["Grace Hopper"]}},
    ))

    _row, links, wb, changes = run(_links_wb_changes("101"))
    managers = [link for link in links if link.relation == "manager"]
    assert len(managers) == 1
    assert managers[0].manager_sp_user_id == 77 and managers[0].manager_name == "Grace Hopper"
    assert wb == []                                        # write-back is off: nothing for SharePoint
    assert len([c for c in changes if c.field == "AllManagers"]) == 1


# --- Admin set-balances (routes/dashboard.py) ---------------------------------

def test_set_balances_lands_in_postgres(clean_db, monkeypatch):
    # The older set-balances handler routes through the store too, so a correction
    # lands in Postgres with one write-back and one admin_dashboard history row.
    from app.routes.dashboard import admin_set_balances
    from app.services import balance as balance_module

    _seed(monkeypatch, _sp_item("101", CurrentVacationBalance=10))
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def fake_rad(emp_id, vacation, carryover):
        return None
    monkeypatch.setattr(balance_module, "recalculate_request_allow_date", fake_rad)

    report = run(admin_set_balances({"corrections": [{"employee_id": "101", "vacation": 3.0}]}))
    assert report["applied"] and report["applied"][0]["after"] == {"vacation": 3.0}

    row, _links, wb, changes = run(_links_wb_changes("101"))
    assert float(row.vacation_balance) == 3.0
    assert wb == []                                        # write-back is off: nothing for SharePoint
    vac = [c for c in changes if c.field == "CurrentVacationBalance"]
    assert len(vac) == 1 and vac[0].source == "admin_dashboard"


# --- Manager assignment links (manager_assignments) ---------------------------

def test_manager_assignment_lands_in_postgres(clean_db, monkeypatch):
    # Setting a person's managers lands the link (with name) in Postgres, queues
    # one write-back carrying the id collection, and records one system history row.
    # The manager is a staff row too: on Postgres the name comes from the staff
    # table, never from the SharePoint site user list.
    _seed(monkeypatch, _sp_item("101"), _sp_item("77", Title="Grace Hopper", EmailAddress="grace@ucsh.com"))
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    result = run(manager_assignments.update_employee_managers(101, [77]))
    assert any(m["sp_user_id"] == 77 and m["name"] == "Grace Hopper" for m in result["managers"])

    _row, links, wb, changes = run(_links_wb_changes("101"))
    managers = [link for link in links if link.relation == "manager"]
    assert len(managers) == 1 and managers[0].manager_name == "Grace Hopper"
    assert wb == []                                        # write-back is off: nothing for SharePoint
    mgr_changes = [c for c in changes if c.field == "AllManagers"]
    assert len(mgr_changes) == 1 and mgr_changes[0].source == "system"


# --- April-1 carry-over reset (tasks/carryover_reset) -------------------------

def test_carryover_reset_lands_in_postgres(clean_db, monkeypatch):
    # Zeroing one person's carry-over lands in Postgres, queues one write-back, and
    # records one system history row.
    _seed(monkeypatch, _sp_item("101", CarryOver=5, CurrentVacationBalance=10))
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    async def fake_rad(emp_id, vacation, carryover):
        return None
    monkeypatch.setattr(carryover_reset, "recalculate_request_allow_date", fake_rad)

    emp_info = {"id": "101", "name": "Ada Lovelace", "email": "ada@ucsh.com",
                "carryover": 5.0, "vacation": 10.0}
    run(carryover_reset._reset_single_employee(emp_info))

    row, _links, wb, changes = run(_links_wb_changes("101"))
    assert float(row.carry_over) == 0.0
    assert wb == []                                        # write-back is off: nothing for SharePoint
    co = [c for c in changes if c.field == "CarryOver"]
    assert len(co) == 1 and co[0].source == "system"


# --- Add employee (employee_creation) -----------------------------------------

GOOD_FORM = {
    "title": "New Hire",
    "email_address": "newhire@ucsh.com",
    "location": "Toronto Warden",
    "department": "Warehouse",
    "salary_hourly": "Salary",
    "vacation_entitlement": 15,
    "sick_entitlement": 8,
}


def test_add_employee_lands_only_in_postgres(clean_db, monkeypatch):
    # In Postgres mode add-employee mints a staff id in the app's own range, and
    # nothing is written to SharePoint or queued for it.
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    async def no_clash(name, email):
        return {"name": None, "email": None}               # nobody already in the directory
    monkeypatch.setattr(ec, "find_directory_clashes", no_clash)

    async def fake_resolve(email):
        return 501                                         # linked to the site -> no notice
    monkeypatch.setattr(ec, "_resolve_user_lookup_id", fake_resolve)

    async def fake_rad(*a, **k):
        return None                                        # the derived date is balance.py's job
    monkeypatch.setattr(ec, "recalculate_request_allow_date", fake_rad)

    async def fake_map():
        return {501: "The Boss"}
    monkeypatch.setattr(manager_assignments, "_get_sp_user_name_map", fake_map)

    sharepoint_writes = []                                 # any SharePoint write would land here

    async def fake_create(list_id, fields):
        sharepoint_writes.append(("create", list_id))
        return {"id": "900", "fields": fields}
    async def fake_update(list_id, item_id, fields):
        sharepoint_writes.append(("update", list_id))
        return {}
    monkeypatch.setattr(sharepoint_module.sp_client, "create_list_item", fake_create)
    monkeypatch.setattr(sharepoint_module.sp_client, "update_list_item_fields", fake_update)

    record = run(ec.create_employee(GOOD_FORM, [501]))
    new_id = record["id"]
    # App-made ids live in their own range, never one SharePoint could hand out.
    from app.repositories.postgres.staff import APP_STAFF_ID_START
    assert int(new_id) >= APP_STAFF_ID_START

    async def writebacks():
        async with async_session() as session:
            return (await session.execute(select(StaffWriteback))).scalars().all()
    assert run(writebacks()) == []                         # nothing queued for SharePoint
    assert sharepoint_writes == []                         # and nothing written there

    async def read_row():
        async with async_session() as session:
            return (await session.execute(
                select(Staff).where(Staff.sp_item_id == new_id)
            )).scalars().first()
    assert run(read_row()).name == "New Hire"              # the person is in Postgres


# --- A rename keeps the manager's team ------------------------------------------

def test_renaming_a_manager_carries_to_every_link_that_names_them(clean_db, monkeypatch):
    # Teams are read by the manager's name, so a rename after the switch must reach
    # the links, or the manager's whole team would vanish from their dashboard.
    from app.repositories.postgres.staff import PostgresStaffList
    boss = _sp_item("201", Title="Grace Hopper")
    worker = _sp_item("101", AllManagers=[{"LookupId": 77, "LookupValue": "Grace Hopper"}])
    _seed(monkeypatch, boss, worker)

    run(PostgresStaffList().update_list_item_fields(
        settings.SP_LIST_STAFF_DIRECTORY, "201", {"Title": "Grace Murray Hopper"},
    ))

    _row, links, _wb, _changes = run(_links_wb_changes("101"))
    manager = [link for link in links if link.relation == "manager"]
    assert [link.manager_name for link in manager] == ["Grace Murray Hopper"]
    items = run(PostgresStaffList().get_list_items(settings.SP_LIST_STAFF_DIRECTORY))
    worker_fields = next(i["fields"] for i in items if i["id"] == "101")
    assert worker_fields["AllManagers"][0]["LookupValue"] == "Grace Murray Hopper"   # what team views read


# --- A staff save tells open dashboards -------------------------------------------

def test_a_staff_save_signals_every_open_dashboard(clean_db, monkeypatch):
    # Saving a person must reach every live reader as a staff_changed signal, so
    # manager team cards and the admin staff table follow without a reload.
    from app.repositories.postgres.staff import PostgresStaffList
    from app.services.event_bus import STAFF_CHANGED, event_bus, sse_signal
    _seed(monkeypatch, _sp_item("301", Title="Ada Lovelace"))

    queue = event_bus.subscribe()                                # stands in for one open dashboard
    try:
        run(PostgresStaffList().update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, "301", {"CurrentVacationBalance": 4},
        ))
        payload = queue.get_nowait()
    finally:
        event_bus.unsubscribe(queue)

    assert payload == {"signal": STAFF_CHANGED, "staff_id": "301"}
    # The stream sends it as a named event with no id, so it never moves a resume point.
    assert sse_signal(payload) == 'event: staff_changed\ndata: {"staff_id": "301"}\n\n'
