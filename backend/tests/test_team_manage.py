"""A manager's control over their own team lands in Postgres and is guarded (#362, #363).

These run on SQLite with ``sp_client`` faked and ``STORAGE_EMPLOYEES=postgres``,
the same shape as ``test_staff_writers_postgres``: the staff directory is imported
into the ``staff`` table, then the ``routes/team_manage`` endpoints are called
directly (they are plain async functions) with a constructed ``DashboardUser``.

What is pinned: a manager edits a teammate's balance and it is recorded under the
manager's name in the ``manager_dashboard`` channel; the same edit is refused for
someone not on their team; a manager adds a person and removes themselves while
another manager stays; and the "does anyone else manage them" flag the remove
confirmation reads. The add/remove go through ``update_employee_managers`` so the
write lands in Postgres like every other staff write.
"""
import asyncio
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint as sharepoint_module
from app.models.legacy_finding import LegacyFinding
from app.models.staff import Staff, StaffSupervisor, StaffWriteback
from app.models.staff_change import StaffChange
from app.models.balance_ledger import BalanceLedger
from app.models.outbox import Outbox
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.routes.dashboard import DashboardUser
from app.routes import dashboard as dash
from app.routes import team_manage as tm
from app.services import staff_import

STAFF = settings.SP_LIST_STAFF_DIRECTORY


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _mgr(staff_id: int, name: str) -> dict:
    """One AllManagers entry as the Postgres staff shape stores it.

    Args:
        staff_id: The manager's own staff id (their AllManagers lookup id after
            the switch).
        name: Their display name.

    Returns:
        The ``{"LookupId", "LookupValue"}`` entry.
    """
    return {"LookupId": staff_id, "LookupValue": name}


def _sp_item(item_id: str, title: str, managers: list[dict], **overrides) -> dict:
    """A Staff Directory item for the fake SharePoint import.

    Args:
        item_id: The SharePoint item id (kept as the staff id after import).
        title: The person's display name.
        managers: Their AllManagers entries.
        **overrides: Any field overrides (e.g. a different sick balance).

    Returns:
        The ``{"id", "fields"}`` SharePoint item shape.
    """
    fields = {
        "Title": title,
        "EmailAddress": f"{title.split(' ')[0].lower()}@ucsh.com",
        "SalaryHourly": "Salary",
        "Location": "Toronto Warden",
        "Department": "Warehouse",
        "DefaultYearlyVacationDays": 15,
        "SickDayEntitlement": 8,
        "CurrentVacationBalance": 10.0,
        "CurrentSickDayBalance": 5,
        "CurrentOvertimeBalance": 0,
        "CarryOver": 0,
        "Payout": 0,
        "Modified": "2026-10-10T12:00:00Z",
        "Created": "2020-01-01T00:00:00Z",
        "AllManagers": managers,
        "SupervisorLinkLookupId": None,
    }
    fields.update(overrides)
    return {"id": str(item_id), "fields": fields}


# The team: two managers, and three people under them in different shapes.
STEVE = _sp_item("42", "Steve Faith", [])
DANA = _sp_item("43", "Dana Quinn", [])
ROLAND = _sp_item("480", "Roland Alvarez", [_mgr(42, "Steve Faith")])      # Steve only
MIA = _sp_item("481", "Mia Chen", [_mgr(42, "Steve Faith"), _mgr(43, "Dana Quinn")])  # both
OMAR = _sp_item("482", "Omar Diaz", [_mgr(43, "Dana Quinn")])              # Dana only, not Steve's
DIRECTORY = [STEVE, DANA, ROLAND, MIA, OMAR]

# Steve, acting through his signed manager link.
STEVE_USER = DashboardUser(role="manager", user_id="42")


@pytest.fixture
def seeded(monkeypatch):
    """Both storages pinned to Postgres, the directory imported, tables clean."""
    async def wipe():
        async with async_session() as session:
            for model in (StaffWriteback, StaffSupervisor, StaffChange, LegacyFinding, Staff):
                await session.execute(delete(model))
            await session.commit()

    async def fake_get_list_items(list_id, **kwargs):
        return [dict(i, fields=dict(i["fields"])) for i in DIRECTORY]   # fresh copies

    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_get_list_items)

    async def import_now():
        async with async_session() as session:
            await staff_import.staff_sync_step(session, None)          # fills the staff table
            await session.commit()

    run(wipe())
    run(import_now())
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")      # reads come from Postgres now
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)          # writes are allowed
    yield
    run(wipe())


async def _changes_for(employee_id: str) -> list[StaffChange]:
    """Every recorded change row for one person, for the history assertions."""
    async with async_session() as session:
        return (await session.execute(
            select(StaffChange).where(StaffChange.employee_id == str(employee_id))
        )).scalars().all()


# --- #362: change a teammate's balances and entitlements ----------------------

def test_a_manager_edits_a_teammates_balance_recorded_with_their_name(seeded):
    # Steve drops Roland's sick balance; the move lands in Postgres and the one
    # history row carries the manager_dashboard channel and names the manager.
    result = run(tm.team_update_entitlements(
        STEVE_USER, "480",
        tm.TeamEntitlementChanges(changes={"sick": 4}, reason="covered a shift"),
    ))
    assert {c["field"] for c in result["changed"]} == {"sick"}

    person = run(tm.get_employee_by_id("480"))
    assert float(person["fields"]["CurrentSickDayBalance"]) == 4.0       # the edit landed

    rows = run(_changes_for("480"))
    sick = [c for c in rows if c.field == "CurrentSickDayBalance"]
    assert len(sick) == 1                                                # logged once
    assert sick[0].source == "manager_dashboard"                         # the manager's channel
    assert "Steve Faith" in sick[0].reason                               # recorded under their name
    assert "covered a shift" in sick[0].reason                           # the manager's own words


def test_an_entitlement_edit_is_refused_for_a_non_teammate(seeded):
    # Omar is Dana's, not Steve's, so Steve cannot touch his balances.
    with pytest.raises(HTTPException) as err:
        run(tm.team_update_entitlements(
            STEVE_USER, "482",
            tm.TeamEntitlementChanges(changes={"sick": 1}, reason="nope"),
        ))
    assert err.value.status_code == 403
    rows = run(_changes_for("482"))
    assert rows == []                                                    # nothing was written


# --- #363: add and remove people on the team ----------------------------------

def test_a_manager_adds_a_person_to_their_team(seeded):
    # Steve adds Omar; Dana stays, Steve joins, and it is recorded under his name.
    result = run(tm.team_add_member(STEVE_USER, "482"))
    assert result["added"] is True

    person = run(tm.get_employee_by_id("482"))
    names = {m["LookupValue"] for m in person["fields"]["AllManagers"]}
    assert names == {"Dana Quinn", "Steve Faith"}                        # both manage Omar now

    rows = run(_changes_for("482"))
    mgr_rows = [c for c in rows if c.field == "AllManagers"]
    assert len(mgr_rows) == 1 and mgr_rows[0].source == "manager_dashboard"
    assert "Steve Faith" in mgr_rows[0].reason


def test_adding_someone_already_on_the_team_is_a_noop(seeded):
    # Roland already lists Steve, so adding him again writes nothing.
    result = run(tm.team_add_member(STEVE_USER, "480"))
    assert result["added"] is False
    assert run(_changes_for("480")) == []


def test_a_manager_removes_themself_and_another_manager_stays(seeded):
    # Steve steps off Mia, who is also Dana's; Dana remains and the response says so.
    result = run(tm.team_remove_member(STEVE_USER, "481"))
    assert result["removed"] is True
    assert result["has_other_manager"] is True                          # Dana is still there

    person = run(tm.get_employee_by_id("481"))
    names = [m["LookupValue"] for m in person["fields"]["AllManagers"]]
    assert names == ["Dana Quinn"]                                       # only Dana left

    rows = run(_changes_for("481"))
    mgr_rows = [c for c in rows if c.field == "AllManagers"]
    assert len(mgr_rows) == 1 and mgr_rows[0].source == "manager_dashboard"
    assert "Steve Faith" in mgr_rows[0].reason


def test_removing_a_non_teammate_is_refused(seeded):
    # Steve does not manage Omar, so he cannot remove himself from Omar.
    with pytest.raises(HTTPException) as err:
        run(tm.team_remove_member(STEVE_USER, "482"))
    assert err.value.status_code == 403


# --- #363: the "no other manager" flag the remove confirmation reads ----------

def test_the_no_other_manager_flag(seeded):
    # Roland is Steve's only: removing Steve would leave nobody, so the flag is False.
    roland = run(tm.team_member_managers(STEVE_USER, "480"))
    assert roland["has_other_manager"] is False
    assert roland["managers"] == ["Steve Faith"]
    # Mia has Dana too, so another manager would remain: the flag is True.
    mia = run(tm.team_member_managers(STEVE_USER, "481"))
    assert mia["has_other_manager"] is True


# --- #363: the addable picker -------------------------------------------------

def test_addable_lists_only_people_not_already_on_the_team(seeded):
    people = run(tm.team_addable(STEVE_USER))["people"]
    names = {p["name"] for p in people}
    assert "Roland Alvarez" not in names and "Mia Chen" not in names    # already Steve's
    assert "Steve Faith" not in names                                    # not himself
    assert {"Dana Quinn", "Omar Diaz"} <= names                         # the rest are addable


# --- the role guard -----------------------------------------------------------

def test_an_employee_token_cannot_reach_team_control(seeded):
    employee = DashboardUser(role="employee", user_id="480")
    with pytest.raises(HTTPException) as err:
        run(tm.team_addable(employee))
    assert err.value.status_code == 403


# --- #362: a manager may never add or edit themselves (owner ruling 2026-10-19) ---

def test_a_manager_cannot_add_themself_to_their_own_team(seeded):
    # Steve's own id: a manager may add anyone active except themselves.
    with pytest.raises(HTTPException) as err:
        run(tm.team_add_member(STEVE_USER, "42"))
    assert err.value.status_code == 403
    assert "yourself" in err.value.detail.lower()


def test_a_manager_cannot_change_their_own_entitlements(seeded):
    # A manager edits any teammate's figures, but never their own.
    with pytest.raises(HTTPException) as err:
        run(tm.team_update_entitlements(
            STEVE_USER, "42",
            tm.TeamEntitlementChanges(changes={"sick": 1}, reason="nope"),
        ))
    assert err.value.status_code == 403
    assert "your own" in err.value.detail.lower()
    assert run(_changes_for("42")) == []                                 # nothing written to his own record


def test_an_archived_teammate_cannot_be_edited_from_a_team_route(seeded):
    # Roland is Steve's, then leaves: an archived person is changed from the admin
    # staff screen, not a team route, so the edit is refused (#119).
    run(_archive("480"))
    with pytest.raises(HTTPException) as err:
        run(tm.team_update_entitlements(
            STEVE_USER, "480",
            tm.TeamEntitlementChanges(changes={"sick": 1}, reason="covered a shift"),
        ))
    assert err.value.status_code == 403
    assert "archived" in err.value.detail.lower()


# --- #362/#369: the dashboard approve/reject own-team gate -----------------------

async def _wipe_requests() -> None:
    """Empty the request tables so a seeded request does not leak between tests."""
    async with async_session() as session:
        for model in (Outbox, BalanceLedger, RequestEvent, RequestChange, RequestRecord, RequestCounter):
            await session.execute(delete(model))
        await session.commit()


async def _archive(staff_id: str) -> None:
    """Mark one seeded staff row archived, so ``is_archived`` sees it (#119)."""
    from app.models.mixins import utcnow
    async with async_session() as session:
        row = (await session.execute(
            select(Staff).where(Staff.sp_item_id == str(staff_id))
        )).scalars().first()
        row.archived_at = utcnow()
        await session.commit()


async def _seed_leave(item_id: int, employee_id: str, manager_id: str) -> None:
    """Insert one pending leave request assigned to ``manager_id`` for the gate tests."""
    async with async_session() as session:
        session.add(RequestRecord(
            kind="leave", item_id=item_id, status="pending",
            employee_id=str(employee_id), manager_id=str(manager_id), manager_name="x",
            leave_type="Vacation", start_date=date(2026, 12, 1), end_date=date(2026, 12, 1),
            days=1.0, approval_version=1, balance_state="not_applied", extra={},
        ))
        await session.commit()


def test_a_manager_cannot_decide_a_request_off_their_team(seeded, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")     # requests_in_postgres() -> True
    run(_wipe_requests())
    run(_seed_leave(90001, employee_id="482", manager_id="43"))       # Omar's request, Dana's team
    with pytest.raises(HTTPException) as err:
        run(dash._refuse_manager_not_on_request(STEVE_USER, "leave", "90001"))
    assert err.value.status_code == 403
    assert "not on your team" in err.value.detail.lower()
    run(_wipe_requests())


def test_a_manager_cannot_decide_their_own_request(seeded, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    run(_wipe_requests())
    run(_seed_leave(90002, employee_id="42", manager_id="43"))        # Steve's own request
    with pytest.raises(HTTPException) as err:
        run(dash._refuse_manager_not_on_request(STEVE_USER, "leave", "90002"))
    assert err.value.status_code == 403
    assert "your own" in err.value.detail.lower()
    run(_wipe_requests())


def test_a_manager_on_the_team_may_decide(seeded, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    run(_wipe_requests())
    # Roland lists Steve; the request is assigned to Dana, so this exercises the
    # "on the employee's manager list" branch, not the assigned-manager shortcut.
    run(_seed_leave(90003, employee_id="480", manager_id="43"))
    run(dash._refuse_manager_not_on_request(STEVE_USER, "leave", "90003"))   # no raise
    run(_wipe_requests())


def test_an_admin_may_decide_any_request(seeded, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    run(_wipe_requests())
    run(_seed_leave(90004, employee_id="482", manager_id="43"))       # off any team Steve has
    admin = DashboardUser(role="admin", user_id="999")
    run(dash._refuse_manager_not_on_request(admin, "leave", "90004"))     # admins are unchanged
    run(_wipe_requests())
