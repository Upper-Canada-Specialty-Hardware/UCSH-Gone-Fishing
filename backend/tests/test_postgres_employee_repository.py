"""Migration PR G: employees can be served from Postgres instead of SharePoint.

The cutover is only safe if a caller cannot tell the two apart, so most of what
is asserted here is *shape*: the SharePoint field names, the synthesised
AllManagers list, and the round-trip of a balance write. The balance engine and
the approval flows read these dicts directly, so a wrong key is a silent
business bug rather than a crash.

Runs against the local SQLite fallback (DATABASE_URL is empty under conftest).
"""
import asyncio
from datetime import date

import pytest

from app.database import Base, async_session, engine
from app.models.employee import Employee
from app.models.manager_assignment import ManagerAssignment
from app.repositories import factory
from app.repositories.postgres.employee import (
    _FIELD_TO_COLUMN,
    PostgresEmployeeRepository,
)
from app.repositories.postgres.manager_assignments import (
    PostgresManagerAssignmentRepository,
)
from app.repositories.sharepoint.employee import SharePointEmployeeRepository

BOSS_LOOKUP_ID = 501
SECOND_LOOKUP_ID = 502


def _seed():
    """Two managers and one employee who reports to both, boss first."""
    async def inner():
        # The local dev SQLite file persists between runs and create_all does not
        # add columns to an existing table — so a stale employees table from a
        # previous run would be missing what Alembic 0006 adds. Drop these two
        # and let create_all rebuild them from the current models.
        assert engine.url.get_backend_name() == "sqlite", (
            "these tests drop tables; they must run against the SQLite fallback "
            "(leave DATABASE_URL empty)"
        )
        async with engine.begin() as conn:
            await conn.run_sync(ManagerAssignment.__table__.drop, checkfirst=True)
            await conn.run_sync(Employee.__table__.drop, checkfirst=True)
            await conn.run_sync(Base.metadata.create_all)
        async with async_session() as session:

            boss = Employee(
                sp_item_id="10", name="Bob Boss", email="bob@example.invalid",
                sp_user_lookup_id=BOSS_LOOKUP_ID, salary_hourly="Salary",
            )
            second = Employee(
                sp_item_id="11", name="Sam Second", email="sam@example.invalid",
                sp_user_lookup_id=SECOND_LOOKUP_ID, salary_hourly="Salary",
            )
            jane = Employee(
                sp_item_id="12",
                name="Jane Doe",
                email="jane@example.invalid",
                cell_number="4165550143",
                department="Operations",
                location="Toronto Victoria Park",
                employee_type="Full Time",
                salary_hourly="Hourly",
                vacation_balance=10.0,
                sick_balance=5.0,
                overtime_balance=1.5,
                carryover_balance=2.0,
                payout_balance=0.0,
                vacation_entitlement=15.0,
                sick_entitlement=6.0,
                request_allow_date=date(2026, 12, 31),
            )
            session.add_all([boss, second, jane])
            await session.commit()

            session.add_all([
                ManagerAssignment(employee_id=jane.id, manager_sp_user_lookup_id=BOSS_LOOKUP_ID,
                                  manager_name="Bob Boss", position=0),
                ManagerAssignment(employee_id=jane.id, manager_sp_user_lookup_id=SECOND_LOOKUP_ID,
                                  manager_name="Sam Second", position=1),
            ])
            await session.commit()

    asyncio.run(inner())


@pytest.fixture
def repo():
    _seed()
    return PostgresEmployeeRepository()


def _jane(repo):
    return asyncio.run(repo.get_by_id("12"))


# --- Shape parity -----------------------------------------------------------

def test_every_mapped_field_is_present_on_a_read(repo):
    fields = _jane(repo)["fields"]
    missing = [name for name in _FIELD_TO_COLUMN if name not in fields]
    assert not missing, f"read is missing SharePoint field(s): {missing}"


# Every Staff Directory field the app reads off an employee record. Sourced by
# grepping `.get("...")` across app/ — the omission of SalaryHourly and
# CellNumber from Alembic 0005 is exactly what this guards against, since a
# missing SalaryHourly reads as None and silently starts deducting vacation
# from hourly staff.
EMPLOYEE_FIELDS_THE_APP_READS = {
    "Title", "EmailAddress", "CellNumber", "Department", "Location",
    "EmployeeType", "SalaryHourly", "CurrentVacationBalance",
    "CurrentSickDayBalance", "CurrentOvertimeBalance", "CarryOver", "Payout",
    "DefaultYearlyVacationDays", "SickDayEntitlement", "RequestAllowDate",
    "AllManagers",
}


def test_the_mapping_covers_every_field_the_app_reads(repo):
    served = set(_FIELD_TO_COLUMN) | {"AllManagers"}
    unmapped = EMPLOYEE_FIELDS_THE_APP_READS - served
    assert not unmapped, (
        f"Postgres would serve None for {sorted(unmapped)}, which callers read. "
        "Add the column (with a migration) and map it."
    )
    # And the read really produces them, not just the mapping table.
    fields = _jane(repo)["fields"]
    assert EMPLOYEE_FIELDS_THE_APP_READS <= set(fields)


def test_field_names_match_what_the_balance_engine_reads(repo):
    fields = _jane(repo)["fields"]
    assert fields["CurrentVacationBalance"] == 10.0
    assert fields["CurrentSickDayBalance"] == 5.0
    assert fields["CurrentOvertimeBalance"] == 1.5
    assert fields["CarryOver"] == 2.0
    assert fields["Payout"] == 0.0


def test_the_two_fields_0006_added_are_readable(repo):
    """SalaryHourly gates balance deduction; CellNumber is the SMS recipient."""
    fields = _jane(repo)["fields"]
    assert fields["SalaryHourly"] == "Hourly"
    assert fields["CellNumber"] == "4165550143"


def test_id_is_the_sharepoint_item_id(repo):
    assert _jane(repo)["id"] == "12"


def test_request_allow_date_is_an_iso_string(repo):
    # balance.recalculate_request_allow_date compares current_rad[:10].
    rad = _jane(repo)["fields"]["RequestAllowDate"]
    assert rad == "2026-12-31"
    assert rad[:10] == "2026-12-31"


def test_absent_date_reads_as_none(repo):
    assert asyncio.run(repo.get_by_id("10"))["fields"]["RequestAllowDate"] is None


# --- AllManagers synthesis --------------------------------------------------

def test_all_managers_is_rebuilt_in_sharepoint_person_field_shape(repo):
    assert _jane(repo)["fields"]["AllManagers"] == [
        {"LookupId": BOSS_LOOKUP_ID, "LookupValue": "Bob Boss"},
        {"LookupId": SECOND_LOOKUP_ID, "LookupValue": "Sam Second"},
    ]


def test_primary_manager_is_the_lowest_position(repo):
    """get_all_managers_for_employee treats managers[0] as the primary."""
    first = _jane(repo)["fields"]["AllManagers"][0]
    assert first["LookupValue"] == "Bob Boss"


def test_lookup_value_is_populated_unlike_graph(repo):
    # get_all_managers_for_employee skips entries with an empty LookupValue,
    # so an unnamed manager would silently drop out of the approval chain.
    for entry in _jane(repo)["fields"]["AllManagers"]:
        assert entry["LookupValue"]


def test_employee_without_managers_gets_an_empty_list(repo):
    assert asyncio.run(repo.get_by_id("10"))["fields"]["AllManagers"] == []


# --- Lookups ----------------------------------------------------------------

def test_get_by_name_is_case_and_whitespace_insensitive(repo):
    assert asyncio.run(repo.get_by_name("  jane doe "))["id"] == "12"


def test_get_by_email_is_case_insensitive(repo):
    assert asyncio.run(repo.get_by_email("JANE@EXAMPLE.INVALID"))["id"] == "12"


@pytest.mark.parametrize("missing", ["", "   ", "nobody@example.invalid"])
def test_lookups_return_none_when_absent(repo, missing):
    assert asyncio.run(repo.get_by_email(missing)) is None


def test_get_by_id_returns_none_when_absent(repo):
    assert asyncio.run(repo.get_by_id("999")) is None


def test_get_all_returns_every_employee(repo):
    assert {e["id"] for e in asyncio.run(repo.get_all())} == {"10", "11", "12"}


# --- Writes -----------------------------------------------------------------

def test_balance_write_round_trips(repo):
    asyncio.run(repo.update_fields("12", {
        "CurrentVacationBalance": 7.5,
        "CarryOver": 0,
    }))
    fields = _jane(repo)["fields"]
    assert fields["CurrentVacationBalance"] == 7.5
    assert fields["CarryOver"] == 0
    # Untouched pots must not move.
    assert fields["CurrentSickDayBalance"] == 5.0


def test_request_allow_date_write_accepts_an_iso_string(repo):
    asyncio.run(repo.update_fields("12", {
        "RequestAllowDate": "2027-03-31", "Title": "Jane Doe",
    }))
    assert _jane(repo)["fields"]["RequestAllowDate"] == "2027-03-31"


def test_unknown_field_raises_rather_than_being_dropped(repo):
    # A silently ignored balance write would be unrecoverable, so this is loud.
    with pytest.raises(KeyError) as excinfo:
        asyncio.run(repo.update_fields("12", {"SomeNewColumn": 1}))
    assert "SomeNewColumn" in str(excinfo.value)


def test_create_round_trips_a_new_hire(repo):
    # build_employee_fields-shaped payload; managers are set separately, so none here.
    created = asyncio.run(repo.create({
        "Title": "New Hire", "EmailAddress": "new@example.invalid",
        "Location": "Toronto Victoria Park", "Department": "Warehouse",
        "SalaryHourly": "Salary", "DefaultYearlyVacationDays": 15.0,
        "SickDayEntitlement": 5.0, "CurrentVacationBalance": 0.0,
    }))
    # The re-read returns the created record in the SharePoint shape.
    assert created["fields"]["Title"] == "New Hire"
    assert created["fields"]["SalaryHourly"] == "Salary"
    assert created["fields"]["AllManagers"] == []  # no managers set on create
    # Fetchable by the minted id afterwards.
    assert asyncio.run(repo.get_by_id(created["id"]))["fields"]["EmailAddress"] == "new@example.invalid"


def test_create_mints_an_id_above_the_current_max(repo):
    # Seed ids are "10","11","12"; the next id must be strictly greater so it
    # cannot collide with a backfilled SharePoint id.
    created = asyncio.run(repo.create({"Title": "Next", "EmailAddress": "next@example.invalid"}))
    assert created["id"] == "13"


def test_create_unknown_field_raises_rather_than_being_dropped(repo):
    with pytest.raises(KeyError) as excinfo:
        asyncio.run(repo.create({"Title": "X", "SomeNewColumn": 1}))
    assert "SomeNewColumn" in str(excinfo.value)


def test_write_to_a_missing_employee_raises(repo):
    with pytest.raises(KeyError):
        asyncio.run(repo.update_fields("999", {"CarryOver": 1}))


def test_all_managers_write_replaces_the_set_and_keeps_order(repo):
    # services/manager_assignments.update_employee_managers sends this shape.
    asyncio.run(repo.update_fields("12", {
        "AllManagersLookupId@odata.type": "Collection(Edm.Int32)",
        "AllManagersLookupId": [SECOND_LOOKUP_ID, BOSS_LOOKUP_ID],
    }))
    assert _jane(repo)["fields"]["AllManagers"] == [
        {"LookupId": SECOND_LOOKUP_ID, "LookupValue": "Sam Second"},
        {"LookupId": BOSS_LOOKUP_ID, "LookupValue": "Bob Boss"},
    ]


def test_all_managers_write_can_clear_them(repo):
    asyncio.run(repo.update_fields("12", {
        "AllManagersLookupId@odata.type": "Collection(Edm.Int32)",
        "AllManagersLookupId": [],
    }))
    assert _jane(repo)["fields"]["AllManagers"] == []


def test_all_managers_write_does_not_duplicate_on_repeat(repo):
    for _ in range(3):
        asyncio.run(repo.update_fields("12", {
            "AllManagersLookupId": [BOSS_LOOKUP_ID],
        }))
    assert len(_jane(repo)["fields"]["AllManagers"]) == 1


# --- Manager assignment repository -----------------------------------------

def test_manager_assignment_repo_matches_the_employee_repo(repo):
    """It delegates, so the two can never disagree about AllManagers."""
    via_assignments = asyncio.run(PostgresManagerAssignmentRepository().get_all_assignments())
    via_employees = asyncio.run(repo.get_all())
    assert via_assignments == via_employees


# --- Factory wiring ---------------------------------------------------------

def test_factory_returns_postgres_when_the_flag_says_so(monkeypatch):
    monkeypatch.setattr(factory.settings, "STORAGE_EMPLOYEES", "postgres")
    assert isinstance(factory.get_employee_repository(), PostgresEmployeeRepository)
    assert isinstance(
        factory.get_manager_assignment_repository(), PostgresManagerAssignmentRepository
    )


def test_factory_still_defaults_to_sharepoint(monkeypatch):
    monkeypatch.setattr(factory.settings, "STORAGE_EMPLOYEES", "sharepoint")
    assert isinstance(factory.get_employee_repository(), SharePointEmployeeRepository)


def test_requests_now_have_a_postgres_backend(monkeypatch):
    # Requests gained a Postgres implementation, so the flag now selects it.
    from app.repositories.postgres.requests import PostgresRequestRepository
    monkeypatch.setattr(factory.settings, "STORAGE_REQUESTS", "postgres")
    assert isinstance(factory.get_leave_request_repository(), PostgresRequestRepository)
