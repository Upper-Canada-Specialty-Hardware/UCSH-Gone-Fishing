"""A manager edits a teammate's full profile, guarded like the admin save (#432).

The owner's ruling: a manager edits every field of a teammate that an admin can
edit in the admin staff profile, email and balances included, with no
restriction; the only difference from an admin is whose records they may edit
(their own team only). These tests pin that: a manager changes a teammate's
email and a balance in one save and both land, recorded under the manager's name
in the ``manager_dashboard`` channel; the same edit is refused for someone not on
their team; and an employee token cannot reach the route at all.

Runs on SQLite with ``sp_client`` faked and ``STORAGE_EMPLOYEES=postgres``, the
same shape as ``test_team_manage``: the staff directory is imported into the
``staff`` table, then ``routes/team_manage.team_update_entitlements`` is called
directly (it is a plain async function) with a constructed ``DashboardUser``.
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
from app.routes.dashboard import DashboardUser
from app.routes import team_manage as tm
from app.services import staff_import

STAFF = settings.SP_LIST_STAFF_DIRECTORY


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _mgr(staff_id: int, name: str) -> dict:
    """One AllManagers entry as the Postgres staff shape stores it.

    Args:
        staff_id: The manager's own staff id (their AllManagers lookup id).
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


# The team: one manager and two people, one on his team and one not.
STEVE = _sp_item("42", "Steve Faith", [])
ROLAND = _sp_item("480", "Roland Alvarez", [_mgr(42, "Steve Faith")])      # Steve's
OMAR = _sp_item("482", "Omar Diaz", [_mgr(43, "Dana Quinn")])              # Dana's, not Steve's
DIRECTORY = [STEVE, ROLAND, OMAR]

# Steve, acting through his signed manager link.
STEVE_USER = DashboardUser(role="manager", user_id="42")


@pytest.fixture
def seeded(monkeypatch):
    """Staff pinned to Postgres, the directory imported, the tables clean."""
    async def wipe():
        async with async_session() as session:
            for model in (StaffWriteback, StaffSupervisor, StaffChange, LegacyFinding, Staff):
                await session.execute(delete(model))
            await session.commit()

    async def fake_get_list_items(list_id, **kwargs):
        return [dict(i, fields=dict(i["fields"])) for i in DIRECTORY]       # fresh copies

    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_get_list_items)

    async def import_now():
        async with async_session() as session:
            await staff_import.staff_sync_step(session, None)              # fills the staff table
            await session.commit()

    run(wipe())
    run(import_now())
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")          # reads come from Postgres now
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)              # writes are allowed
    yield
    run(wipe())


async def _changes_for(employee_id: str) -> list[StaffChange]:
    """Every recorded change row for one person, for the history assertions."""
    async with async_session() as session:
        return (await session.execute(
            select(StaffChange).where(StaffChange.employee_id == str(employee_id))
        )).scalars().all()


def test_a_manager_edits_a_teammates_email_and_balance(seeded):
    # Steve changes Roland's email and sick balance in one save; both land in
    # Postgres and each history row carries the manager_dashboard channel and
    # names the manager (#432).
    result = run(tm.team_update_entitlements(
        STEVE_USER, "480",
        tm.TeamEntitlementChanges(
            changes={"sick": 4}, fields={"email": "roland.new@ucsh.com"},
            reason="new address and a covered shift",
        ),
    ))
    changed = {c["field"] for c in result["changed"]}
    assert changed == {"sick", "email"}                                    # both edits applied

    person = run(tm.get_employee_by_id("480"))
    assert float(person["fields"]["CurrentSickDayBalance"]) == 4.0         # the balance moved
    assert person["fields"]["EmailAddress"] == "roland.new@ucsh.com"       # the email moved

    rows = run(_changes_for("480"))
    email = [c for c in rows if c.field == "EmailAddress"]
    assert len(email) == 1                                                  # logged once
    assert email[0].source == "manager_dashboard"                          # the manager's channel
    assert "Steve Faith" in email[0].reason                                # recorded under their name


def test_a_profile_edit_is_refused_for_a_non_teammate(seeded):
    # Omar is Dana's, not Steve's, so Steve cannot touch his profile.
    with pytest.raises(HTTPException) as err:
        run(tm.team_update_entitlements(
            STEVE_USER, "482",
            tm.TeamEntitlementChanges(fields={"email": "omar.new@ucsh.com"}, reason="nope"),
        ))
    assert err.value.status_code == 403
    assert run(_changes_for("482")) == []                                  # nothing was written


def test_an_employee_token_cannot_edit_a_profile(seeded):
    # The role guard refuses an employee token before any team or field check.
    employee = DashboardUser(role="employee", user_id="480")
    with pytest.raises(HTTPException) as err:
        run(tm.team_update_entitlements(
            employee, "480",
            tm.TeamEntitlementChanges(fields={"email": "x@ucsh.com"}, reason="nope"),
        ))
    assert err.value.status_code == 403
