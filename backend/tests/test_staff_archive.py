"""Archiving a departed employee (#119), on the Postgres staff backend.

These pin staff to Postgres (``STORAGE_EMPLOYEES=postgres``) and seed the staff
table the way production does, through the shadow sync's import of a faked
SharePoint list. They cover the archive/restore write and its history, the
filtering that drops an archived person from every listing and team while a
single-id lookup still resolves them (so past requests keep their name), the
intake refusal, and that an archived person is never chosen as an approving
manager.

They run on SQLite with ``sp_client`` faked, like ``test_staff_writers_postgres``.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint as sharepoint_module
from app.models.legacy_finding import LegacyFinding
from app.models.staff import Staff, StaffSupervisor, StaffWriteback
from app.models.staff_change import StaffChange
from app.repositories.staff_store import staff_store
from app.routes import intake as intake_route
from app.routes.dashboard import (
    DashboardUser,
    _build_staff_lookups,
    _resolve_employee_name,
    _team_ids_including_archived,
    refuse_if_archived_caller,
    team_balances,
    team_members,
)
from app.services import email_codes, staff_import
from app.services.employee import get_all_managers_for_employee, get_employee_by_id
from app.services.staff_archive import (
    ArchiveError,
    archive_employee,
    list_archived,
    restore_employee,
)

STAFF = settings.SP_LIST_STAFF_DIRECTORY


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


def _sp_item(item_id, title, **overrides):
    """A sample SharePoint Staff Directory item, named, with field overrides."""
    fields = {
        "Title": title,
        "EmailAddress": f"{title.split()[0].lower()}@ucsh.com",
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
        "Modified": "2026-10-10T12:00:00Z",
        "Created": "2020-01-01T00:00:00Z",
        "AllManagers": [],
        "SupervisorLinkLookupId": None,
    }
    fields.update(overrides)
    return {"id": str(item_id), "fields": fields}


def _reports_to(manager_sp_user_id, manager_name):
    """An AllManagers person-column blob naming one manager, for a report's item."""
    return [{"LookupId": manager_sp_user_id, "LookupValue": manager_name}]


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
    """Import the given SharePoint staff items into Postgres and pin staff there."""
    async def fake_get_list_items(list_id, **kwargs):
        return list(items) if list_id == STAFF else []    # only the staff list has rows
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_get_list_items)
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")

    async def inner():
        async with async_session() as session:
            await staff_import.staff_sync_step(session, None)
            await session.commit()
    run(inner())


async def _staff_row(sp_id):
    """The Staff row for one person, for archive-column assertions."""
    async with async_session() as session:
        return (await session.execute(
            select(Staff).where(Staff.sp_item_id == str(sp_id))
        )).scalars().first()


async def _changes(sp_id):
    """A person's staff_changes rows, newest first."""
    async with async_session() as session:
        return (await session.execute(
            select(StaffChange).where(StaffChange.employee_id == str(sp_id))
            .order_by(StaffChange.id.desc())
        )).scalars().all()


# --- archive and restore, with history ---------------------------------------

def test_archive_then_restore_writes_the_flag_and_history(clean_db, monkeypatch):
    # Archiving sets the three columns and records one readable history row with
    # the reason; restoring clears the columns and records the reverse row.
    _seed(monkeypatch, _sp_item("202", "Departed Dan"))

    result = run(archive_employee("202", "left the company on Oct 31"))
    assert result["employee_id"] == "202"
    assert result["pending_requests"] == []                # nothing pending to list

    row = run(_staff_row("202"))
    assert row.archived_at is not None                     # the person is now archived
    assert row.archived_reason == "left the company on Oct 31"
    assert row.archived_by == "admin"

    changes = run(_changes("202"))
    assert changes[0].field == "Archived"
    assert changes[0].new_value == "Yes"
    assert changes[0].reason == "left the company on Oct 31"
    assert changes[0].source == "admin_dashboard"

    run(restore_employee("202"))
    row = run(_staff_row("202"))
    assert row.archived_at is None                         # restored: active again
    assert row.archived_reason is None
    changes = run(_changes("202"))
    assert changes[0].field == "Archived" and changes[0].new_value == "No"   # newest row is the restore


def test_archive_refuses_blank_reason_and_double_archive(clean_db, monkeypatch):
    _seed(monkeypatch, _sp_item("202", "Departed Dan"))
    with pytest.raises(ArchiveError):
        run(archive_employee("202", "   "))                # a reason is required
    run(archive_employee("202", "left"))
    with pytest.raises(ArchiveError):
        run(archive_employee("202", "again"))              # already archived


def test_restore_refuses_a_person_who_is_not_archived(clean_db, monkeypatch):
    _seed(monkeypatch, _sp_item("202", "Departed Dan"))
    with pytest.raises(ArchiveError):
        run(restore_employee("202"))


# --- filtered out of listings, kept for single lookups ------------------------

def test_archived_absent_from_listings_but_resolvable_by_id(clean_db, monkeypatch):
    _seed(monkeypatch, _sp_item("201", "Active Alice"), _sp_item("202", "Departed Dan"))
    run(archive_employee("202", "left"))

    active = run(staff_store.get_list_items(STAFF))        # the default read: active only
    ids = {i["id"] for i in active}
    assert "201" in ids and "202" not in ids               # the archived person is gone

    everyone = run(staff_store.get_list_items(STAFF, include_archived=True))
    assert {"201", "202"} <= {i["id"] for i in everyone}   # include_archived brings them back

    one = run(staff_store.get_list_item(STAFF, "202"))     # single lookup is never filtered
    assert one["fields"]["Title"] == "Departed Dan"        # so a past request still resolves the name

    archived = run(list_archived())
    assert [a["id"] for a in archived] == ["202"]
    assert archived[0]["archived_reason"] == "left"


def test_archived_absent_from_team_members_and_balances(clean_db, monkeypatch):
    # Manager Maria has two reports; after Dan is archived he drops out of both
    # the team members and the team balances, while Alice stays.
    _seed(
        monkeypatch,
        _sp_item("200", "Manager Maria"),
        _sp_item("201", "Active Alice", AllManagers=_reports_to(200, "Manager Maria")),
        _sp_item("202", "Departed Dan", AllManagers=_reports_to(200, "Manager Maria")),
    )
    run(archive_employee("202", "left"))
    maria = DashboardUser(role="manager", user_id="200")

    members = run(team_members(maria))["members"]
    assert {m["name"] for m in members} == {"Active Alice"}

    balances = run(team_balances(maria))["balances"]
    assert {b["employee"]["name"] for b in balances} == {"Active Alice"}


# --- still resolvable in request rows, out of team composition ----------------

def test_archived_name_resolves_in_request_rows_but_not_in_team_map(clean_db, monkeypatch):
    _seed(
        monkeypatch,
        _sp_item("200", "Manager Maria"),
        _sp_item("201", "Active Alice", AllManagers=_reports_to(200, "Manager Maria")),
        _sp_item("202", "Departed Dan", AllManagers=_reports_to(200, "Manager Maria")),
    )
    run(archive_employee("202", "left"))

    by_name, by_id, sp_user_to_name, mgr_to_emp_names = run(_build_staff_lookups())
    # The archived person stays in the name/id maps, so a past request keeps its name.
    assert by_id[202]["fields"]["Title"] == "Departed Dan"
    assert sp_user_to_name.by_staff_id["202"] == "Departed Dan"
    assert _resolve_employee_name({"EmployeeID": "202"}, "leave", sp_user_to_name, by_id) == "Departed Dan"
    # But they are no longer an active report, so they are out of the team map.
    assert "Active Alice" in mgr_to_emp_names.get("Manager Maria", set())
    assert "Departed Dan" not in mgr_to_emp_names.get("Manager Maria", set())


# --- never chosen as an approving manager -------------------------------------

def test_archived_manager_is_never_chosen_as_an_approver(clean_db, monkeypatch):
    _seed(
        monkeypatch,
        _sp_item("200", "Manager Maria"),
        _sp_item("201", "Active Alice", AllManagers=_reports_to(200, "Manager Maria")),
    )
    run(archive_employee("200", "left"))                   # the manager departs
    alice = run(get_employee_by_id("201"))
    managers = run(get_all_managers_for_employee(alice))   # resolves each AllManagers name
    assert managers == []                                  # the archived manager is not found, so not chosen


# --- intake refuses an archived person ----------------------------------------

def test_intake_refuses_an_archived_person(clean_db, monkeypatch):
    _seed(monkeypatch, _sp_item("202", "Departed Dan"))
    run(archive_employee("202", "left"))

    async def ok_code(email, code):
        return email_codes.CheckResult(ok=True)            # the code is right; identity is the test
    monkeypatch.setattr(email_codes, "check_code", ok_code)

    body = intake_route.VerifyRequest(email="departed@ucsh.com", code="123456")
    with pytest.raises(HTTPException) as excinfo:
        run(intake_route.verify_code(body))
    assert excinfo.value.status_code == 403                # archived, refused with a plain message


def test_intake_still_signs_in_an_active_person(clean_db, monkeypatch):
    # The archived refusal must not catch an active employee: a normal sign-in works.
    _seed(monkeypatch, _sp_item("201", "Active Alice"))

    async def ok_code(email, code):
        return email_codes.CheckResult(ok=True)
    monkeypatch.setattr(email_codes, "check_code", ok_code)

    body = intake_route.VerifyRequest(email="active@ucsh.com", code="123456")
    answer = run(intake_route.verify_code(body))
    assert answer["status"] == "employee"
    assert answer["name"] == "Active Alice"


# --- an archived person's own dashboard link stops working --------------------

def test_an_archived_employee_or_manager_link_is_refused(clean_db, monkeypatch):
    # A departed person's signed link keeps validating, so the dashboard auth
    # dependency refuses an archived employee or manager by their own id (#119).
    _seed(monkeypatch, _sp_item("201", "Active Alice"), _sp_item("202", "Departed Dan"))
    run(archive_employee("202", "left"))

    with pytest.raises(HTTPException) as err:
        run(refuse_if_archived_caller("employee", "202"))        # their employee link
    assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        run(refuse_if_archived_caller("manager", "202"))         # and their manager link

    run(refuse_if_archived_caller("employee", "201"))            # an active person passes
    run(refuse_if_archived_caller("admin", "202"))               # admin is a shared login, never checked


# --- the manager History team keeps archived teammates ------------------------

def test_the_history_team_map_keeps_archived_teammates(clean_db, monkeypatch):
    # Today/Team/balances use the active-only map, but History scopes by a team map
    # that keeps archived teammates, so their past requests still show (#119).
    _seed(
        monkeypatch,
        _sp_item("200", "Manager Maria"),
        _sp_item("201", "Active Alice", AllManagers=_reports_to(200, "Manager Maria")),
        _sp_item("202", "Departed Dan", AllManagers=_reports_to(200, "Manager Maria")),
    )
    run(archive_employee("202", "left"))

    _by_name, by_id, _sp_user_to_name, mgr_to_emp_names = run(_build_staff_lookups())
    # The active-only map (Today/Team/pickers/balances) drops the archived person.
    assert "Departed Dan" not in mgr_to_emp_names.get("Manager Maria", set())
    # The history team map keeps both, so Dan's past requests stay on Maria's History.
    team = _team_ids_including_archived(by_id, "Manager Maria")
    assert {"201", "202"} <= team
