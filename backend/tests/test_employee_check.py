"""The add-employee "check" step runs every create check without writing.

check_employee is what the Review button calls: it cleans the typed details,
reports what cleaning changed, and lists the blocks and warnings the create
would hit. These pin which problems are blocks and which are warnings, that the
cleaned form and its changes come back, and that nothing is ever written.
"""

import asyncio

from app.services import employee_creation as ec
from app.services import holidays as holidays_module

GOOD = {
    "title": "New Hire",
    "email_address": "newhire@ucsh.com",
    "location": "Toronto Warden",
    "department": "Warehouse",
    "salary_hourly": "Salary",
    "vacation_entitlement": 15,
    "sick_entitlement": 5,
}


def _wire(monkeypatch, *, name_clash=False, email_clash=False, holidays=True):
    """Fake every read check_employee makes, and fail loudly on any write."""
    async def _clashes(name, email):
        return {
            "name": {"id": "9", "fields": {}} if name_clash else None,
            "email": {"id": "8", "fields": {}} if email_clash else None,
        }

    async def _holidays(province):
        return [{"Title": "Canada Day"}] if holidays else []

    async def _no_write(*a, **k):
        raise AssertionError("check_employee must not write")

    monkeypatch.setattr(ec, "find_directory_clashes", _clashes)
    monkeypatch.setattr(holidays_module, "get_holidays_for_province", _holidays)
    # Any write would be a bug: a check never creates or edits a record.
    monkeypatch.setattr(ec.staff_store, "create_list_item", _no_write)
    monkeypatch.setattr(ec.staff_store, "update_list_item_fields", _no_write)


def _codes(result, level):
    """The problem codes at a given level ('block' or 'warn')."""
    return {p["code"] for p in result["problems"] if p["level"] == level}


def test_a_clean_supervised_linked_form_has_no_problems(monkeypatch):
    _wire(monkeypatch)
    result = asyncio.run(ec.check_employee(GOOD, has_supervisor=True, supervisor_unlinked=False))
    assert result["problems"] == []
    assert result["changes"] == []
    assert result["cleaned"]["email_address"] == "newhire@ucsh.com"


def test_an_invalid_form_is_the_only_block_and_short_circuits(monkeypatch):
    _wire(monkeypatch)
    # No location -> build_employee_fields raises, so nothing else is checked.
    result = asyncio.run(ec.check_employee(
        {**GOOD, "location": ""}, has_supervisor=True, supervisor_unlinked=False,
    ))
    assert _codes(result, "block") == {"invalid_form"}


def test_a_duplicate_name_and_email_are_blocks(monkeypatch):
    _wire(monkeypatch, name_clash=True, email_clash=True)
    result = asyncio.run(ec.check_employee(GOOD, has_supervisor=True, supervisor_unlinked=False))
    assert {"same_name", "same_email"} <= _codes(result, "block")


def test_no_supervisor_is_a_block(monkeypatch):
    _wire(monkeypatch)
    result = asyncio.run(ec.check_employee(GOOD, has_supervisor=False, supervisor_unlinked=False))
    assert "no_supervisor" in _codes(result, "block")


def test_the_soft_notices_are_warnings(monkeypatch):
    # Supervisor not linked and no holidays warn; an email not on the SharePoint
    # site is no longer a notice at all (ruled 2026-10-22).
    _wire(monkeypatch, holidays=False)
    result = asyncio.run(ec.check_employee(GOOD, has_supervisor=True, supervisor_unlinked=True))
    assert _codes(result, "warn") == {"supervisor_unlinked", "no_holidays"}
    assert _codes(result, "block") == set()


def test_the_cleaned_form_and_changes_come_back(monkeypatch):
    _wire(monkeypatch)
    result = asyncio.run(ec.check_employee(
        {**GOOD, "title": "Glenn  Clarke ", "email_address": "Glenn@UCSH.com"},
        has_supervisor=True, supervisor_unlinked=False,
    ))
    assert result["cleaned"]["title"] == "Glenn Clarke"
    assert result["cleaned"]["email_address"] == "glenn@ucsh.com"
    fields_changed = {c["field"] for c in result["changes"]}
    assert {"title", "email_address"} <= fields_changed
