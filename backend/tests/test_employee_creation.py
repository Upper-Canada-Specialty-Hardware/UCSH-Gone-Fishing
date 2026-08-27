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


# ----- the async wrapper -----

def _patch(monkeypatch, *, name_exists=False, email_resolves=True):
    """Fake the wrapper's seam + identity touchpoints and capture the writes."""
    calls = {"created": None, "managers": None, "rad": None}

    async def _get_by_name(name):
        return {"id": "9", "fields": {"Title": name}} if name_exists else None

    async def _resolve(email):
        return 501 if email_resolves else None

    class _FakeRepo:
        # Stand-in for the employee repository: records the create and serves
        # the re-read from what was written, so nothing here touches SharePoint.
        async def create(self, fields):
            calls["created"] = fields
            return {"id": "777", "fields": fields}

        async def get_by_id(self, item_id):
            return {"id": str(item_id), "fields": calls["created"] or {}}

    async def _update_managers(emp_id, ids):
        calls["managers"] = (emp_id, ids)

    async def _rad(emp_id, vacation, carryover):
        calls["rad"] = (emp_id, vacation, carryover)

    monkeypatch.setattr(ec, "get_employee_by_name", _get_by_name)
    monkeypatch.setattr(ec, "_resolve_user_lookup_id", _resolve)
    monkeypatch.setattr(ec, "get_employee_repository", lambda: _FakeRepo())  # code writes/reads through the seam
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
    assert calls["rad"] == ("777", 0, 0)


def test_a_duplicate_name_is_refused_before_anything_is_written(monkeypatch):
    calls = _patch(monkeypatch, name_exists=True)

    with pytest.raises(EmployeeValidationError, match="already exists"):
        asyncio.run(create_employee(GOOD, [501]))

    assert calls["created"] is None


def test_an_email_that_does_not_resolve_is_refused(monkeypatch):
    calls = _patch(monkeypatch, email_resolves=False)

    with pytest.raises(EmployeeValidationError, match="Microsoft 365"):
        asyncio.run(create_employee(GOOD, [501]))

    assert calls["created"] is None


def test_no_supervisor_is_refused(monkeypatch):
    calls = _patch(monkeypatch)

    with pytest.raises(EmployeeValidationError, match="supervisor"):
        asyncio.run(create_employee(GOOD, []))

    assert calls["created"] is None
