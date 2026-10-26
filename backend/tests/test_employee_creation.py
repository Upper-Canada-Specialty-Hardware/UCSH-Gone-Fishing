"""Creating an employee must produce a row that actually works.

Adding an employee today is a hand-typed row in the Staff Directory list, which
can be missing anything — and a request from a half-configured person then
stalls with no visible cause. This service is the guided version, so these pin
that it refuses the inputs that would produce a broken row and assembles exactly
the columns the code reads.

The pure assembly-and-validation half is tested directly; the async wrapper's
identity resolution and writes are faked, so nothing here touches SharePoint.
"""

import asyncio

import pytest

from app.graph import sharepoint as sharepoint_module
from app.services import employee_creation as ec
from app.services.employee_creation import (
    EmployeeValidationError,
    build_employee_fields,
    create_employee,
)

GOOD = {
    "title": "New Hire",
    "email_address": "newhire@ucsh.com",
    "location": "Toronto Warden",
    "department": "Warehouse",
    "salary_hourly": "Salary",
    "vacation_entitlement": 15,
    "sick_entitlement": 5,
}


# ----- the pure validation / assembly half -----

def test_a_complete_form_assembles_every_field_the_code_reads():
    fields = build_employee_fields(GOOD)

    assert fields["Title"] == "New Hire"
    assert fields["EmailAddress"] == "newhire@ucsh.com"
    assert fields["Location"] == "Toronto Warden"
    assert fields["Department"] == "Warehouse"
    assert fields["SalaryHourly"] == "Salary"
    assert fields["DefaultYearlyVacationDays"] == 15
    assert fields["SickDayEntitlement"] == 5
    # Every pot present, defaulting to zero.
    for pot in ("CurrentVacationBalance", "CurrentSickDayBalance",
                "CurrentOvertimeBalance", "CarryOver", "Payout"):
        assert fields[pot] == 0


def test_deprecated_columns_are_never_written():
    # These have zero code references; the form must not resurrect them.
    fields = build_employee_fields(GOOD)
    for dead in ("Supervisor", "SupervisorLink", "TitleLink", "System Check",
                 "SystemCheck", "Comments", "Extension", "Birthday"):
        assert dead not in fields


def test_opening_balances_can_be_seeded_for_a_mid_year_hire():
    fields = build_employee_fields({**GOOD, "vacation_balance": 7.5, "carry_over": 2})
    assert fields["CurrentVacationBalance"] == 7.5
    assert fields["CarryOver"] == 2


def test_a_cell_number_is_kept_only_when_given():
    assert "CellNumber" not in build_employee_fields(GOOD)
    assert build_employee_fields({**GOOD, "cell_number": "9051234567"})["CellNumber"] == "9051234567"


@pytest.mark.parametrize("missing", ["title", "email_address", "department"])
def test_a_missing_required_text_field_is_refused(missing):
    with pytest.raises(EmployeeValidationError):
        build_employee_fields({**GOOD, missing: ""})


def test_an_unmapped_location_is_refused():
    # A location with no province blocks day calculation, so it cannot be saved.
    with pytest.raises(EmployeeValidationError, match="Location"):
        build_employee_fields({**GOOD, "location": "Mars"})


def test_an_unknown_employment_type_is_refused():
    # The balance engine branches on the exact string, so only the two values pass.
    with pytest.raises(EmployeeValidationError, match="Employment type"):
        build_employee_fields({**GOOD, "salary_hourly": "Contractor"})


@pytest.mark.parametrize("field", ["vacation_entitlement", "sick_entitlement"])
def test_a_zero_entitlement_is_refused(field):
    with pytest.raises(EmployeeValidationError, match="above zero"):
        build_employee_fields({**GOOD, field: 0})


def test_a_non_numeric_balance_is_refused():
    with pytest.raises(EmployeeValidationError, match="number"):
        build_employee_fields({**GOOD, "vacation_balance": "lots"})


# ----- the identity fields are cleaned on assembly -----

def test_names_and_emails_are_tidied_when_assembled():
    # A trailing space, a doubled space and a non-breaking space in the name all
    # collapse; the email loses its spaces and folds to lower case. This is the
    # Glenn Clarke case: "Glenn Clarke " would otherwise never match.
    fields = build_employee_fields({
        **GOOD,
        "title": "  Glenn  Clarke ",
        "email_address": "  Glenn@UCSH.com ",
        "department": "  Ware   house ",
    })
    assert fields["Title"] == "Glenn Clarke"
    assert fields["EmailAddress"] == "glenn@ucsh.com"
    assert fields["Department"] == "Ware house"


def test_a_name_that_is_only_whitespace_is_refused():
    # Collapsing a space-only name leaves "", which is still no name.
    with pytest.raises(EmployeeValidationError):
        build_employee_fields({**GOOD, "title": "   "})


# ----- loose duplicate detection -----

def test_find_directory_clashes_matches_despite_spacing_and_case(monkeypatch):
    stored = [{
        "id": "1",
        "fields": {"Title": "Glenn Clarke", "EmailAddress": "glenn@ucsh.com"},
    }]

    async def _items(list_id):
        return stored

    monkeypatch.setattr(ec.staff_store, "get_list_items", _items)

    # A doubled space in the name and a capitalised email still resolve to #1.
    found = asyncio.run(ec.find_directory_clashes("glenn  clarke", "GLENN@ucsh.com"))
    assert found["name"]["id"] == "1"
    assert found["email"]["id"] == "1"

    # A genuinely different person clashes with neither.
    clear = asyncio.run(ec.find_directory_clashes("Dana Reed", "dana@ucsh.com"))
    assert clear["name"] is None and clear["email"] is None


def test_a_loosely_duplicate_name_is_refused_before_anything_is_written(monkeypatch):
    # The stored record carries a trailing space; the typed one ("New Hire")
    # does not. They are the same person, so the real loose lookup must catch it
    # and the create must refuse before writing anything.
    wrote = {"created": False}

    async def _items(list_id, **kwargs):
        return [{"id": "1", "fields": {"Title": "New Hire ", "EmailAddress": "other@ucsh.com"}}]

    async def _create(list_id, fields):
        wrote["created"] = True
        return {"id": "777", "fields": fields}

    monkeypatch.setattr(ec.staff_store, "get_list_items", _items)
    monkeypatch.setattr(sharepoint_module.sp_client, "create_list_item", _create)

    with pytest.raises(EmployeeValidationError, match="already exists"):
        asyncio.run(create_employee(GOOD, [501]))

    assert wrote["created"] is False


# ----- the async wrapper -----

def _patch(monkeypatch, *, name_exists=False, email_exists=False):
    """Fake the wrapper's SharePoint touchpoints and capture the writes."""
    calls = {"created": None, "managers": None, "rad": None}

    async def _clashes(name, email):
        # The single loose duplicate lookup create_employee now makes: one hit
        # per flag, in the {"id", "fields"} record shape it returns.
        return {
            "name": {"id": "9", "fields": {"Title": name}} if name_exists else None,
            "email": {"id": "8", "fields": {"EmailAddress": email}} if email_exists else None,
        }

    async def _create(list_id, fields):
        calls["created"] = fields
        return {"id": "777", "fields": fields}

    async def _update_managers(emp_id, ids, *, history_source="system", history_reason=None, history_actor=None):
        calls["managers"] = (emp_id, ids)
        calls["manager_history"] = (history_source, history_actor)   # who/where the assignment was recorded (#447)

    async def _rad(emp_id, vacation, carryover):
        calls["rad"] = (emp_id, vacation, carryover)

    async def _get_item(list_id, item_id):
        return {"id": str(item_id), "fields": calls["created"] or {}}

    monkeypatch.setattr(ec, "find_directory_clashes", _clashes)
    # Default storage is SharePoint, so the store delegates to the shared sp_client
    # object; patch it there so the create and re-read are faked in that mode.
    monkeypatch.setattr(sharepoint_module.sp_client, "create_list_item", _create)
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_item", _get_item)
    monkeypatch.setattr(ec, "update_employee_managers", _update_managers)
    monkeypatch.setattr(ec, "recalculate_request_allow_date", _rad)
    return calls


def test_creating_writes_the_record_then_the_manager_and_the_allow_date(monkeypatch):
    calls = _patch(monkeypatch)

    asyncio.run(create_employee(GOOD, [501]))

    # The row is written first, then the supervisor as a Person field, then the
    # allow date is derived from the opening vacation and carry-over.
    assert calls["created"]["Title"] == "New Hire"
    assert calls["managers"] == (777, [501])
    # The admin create records the first supervisor as HR on the admin channel,
    # not the automatic "system" default (#447).
    assert calls["manager_history"] == ("admin_dashboard", "HR")
    assert calls["rad"] == ("777", 0, 0)


def test_a_duplicate_name_is_refused_before_anything_is_written(monkeypatch):
    calls = _patch(monkeypatch, name_exists=True)

    with pytest.raises(EmployeeValidationError, match="already exists"):
        asyncio.run(create_employee(GOOD, [501]))

    assert calls["created"] is None


def test_a_new_employee_carries_no_notice(monkeypatch):
    # The SharePoint-site notice is gone (ruled 2026-10-22); the key stays empty.
    _patch(monkeypatch)
    assert asyncio.run(create_employee(GOOD, [501]))["notices"] == []


def test_a_duplicate_email_is_refused_before_anything_is_written(monkeypatch):
    calls = _patch(monkeypatch, email_exists=True)

    with pytest.raises(EmployeeValidationError, match="already exists"):
        asyncio.run(create_employee(GOOD, [501]))

    assert calls["created"] is None


def test_no_supervisor_is_refused(monkeypatch):
    calls = _patch(monkeypatch)

    with pytest.raises(EmployeeValidationError, match="supervisor"):
        asyncio.run(create_employee(GOOD, []))

    assert calls["created"] is None
