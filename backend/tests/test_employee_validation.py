"""Tests for the read-only employee-setup validation suite (GH #41).

Two things are worth pinning automatically:
  1. The pure core `build_validation_report` maps each broken/healthy Staff
     Directory value to the right pass/warn/fail check.
  2. The module is SIDE-EFFECT FREE — it must never call a writer/notifier
     (create/update list item, send email/SMS). This is the whole safety
     premise: validating an employee must not fire a real request or notify
     anyone. The guard uses AST so it can't be fooled by the docstring merely
     naming those functions.

The thin async wrapper `validate_employee_setup` is glue over SharePoint reads;
it's exercised via the /admin/validate-employee endpoint and the dashboard UI
(against live SharePoint, with permission), not mocked here.
"""

import ast
import inspect
from datetime import date

from app.services import employee_validation as ev
from app.services.employee_validation import build_validation_report


GOOD_FIELDS = {
    "Title": "Test Employee",
    "Location": "Barrie",              # maps to ON
    "EmailAddress": "test@ucsh.ca",
    "CurrentVacationBalance": 10,
    "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2,
    "CarryOver": 0,
    "Payout": 0,
    "AllManagers": [{"LookupId": 5, "LookupValue": "Boss"}],
}

MANAGERS = [{"id": "5", "fields": {"Title": "Boss", "EmailAddress": "boss@ucsh.ca"}}]
HOLIDAYS = [{"Title": "Canada Day", "Date": "2026-07-01", "Province": "ON"}]
PASS_IDENTITY = {"status": "pass", "detail": "round-trip ok"}
SAMPLE = (date(2026, 7, 6), date(2026, 7, 7))  # a Monday..Tuesday


def _report(**overrides):
    kwargs = dict(
        employee_id="1",
        employee_name="Test Employee",
        employee_fields=GOOD_FIELDS,
        managers=MANAGERS,
        all_managers_count=1,
        identity=PASS_IDENTITY,
        province="ON",
        province_error=None,
        holidays=HOLIDAYS,
        sample_start=SAMPLE[0],
        sample_end=SAMPLE[1],
    )
    kwargs.update(overrides)
    return build_validation_report(**kwargs)


def _by_code(report, code):
    return next(c for c in report["checks"] if c["code"] == code)


# ----- healthy record -----

def test_healthy_employee_all_pass():
    report = _report()
    assert report["overall"] == "pass"
    assert all(c["status"] != "fail" for c in report["checks"])
    # the projected balance is surfaced for a real simulation
    assert _by_code(report, "sim_vacation")["projected"]["CurrentOvertimeBalance"] == 1.0


# ----- identity -----

def test_identity_failure_propagates():
    report = _report(identity={"status": "fail", "detail": "email not in M365"})
    assert _by_code(report, "identity_roundtrip")["status"] == "fail"
    assert report["overall"] == "fail"


# ----- supervisor / AllManagers -----

def test_empty_all_managers_fails():
    report = _report(managers=[], all_managers_count=0,
                     employee_fields={**GOOD_FIELDS, "AllManagers": []})
    assert _by_code(report, "supervisor_set")["status"] == "fail"
    assert report["overall"] == "fail"


def test_unresolved_manager_fails():
    # 2 listed, only 1 resolved -> routing incomplete
    report = _report(all_managers_count=2, managers=MANAGERS)
    assert _by_code(report, "supervisor_resolves")["status"] == "fail"


def test_manager_without_email_warns():
    report = _report(managers=[{"id": "5", "fields": {"Title": "Boss", "EmailAddress": ""}}])
    assert _by_code(report, "manager_reachable")["status"] == "warn"


# ----- location / province -----

def test_bad_location_fails():
    report = _report(province=None,
                     province_error="Province cannot be determined for location: Mars",
                     holidays=[])
    assert _by_code(report, "location_province")["status"] == "fail"
    assert report["overall"] == "fail"


def test_no_holidays_warns():
    report = _report(holidays=[])
    assert _by_code(report, "holidays_load")["status"] == "warn"


# ----- balances -----

def test_non_numeric_balance_fails():
    bad = {**GOOD_FIELDS, "CurrentVacationBalance": "N/A"}
    report = _report(employee_fields=bad)
    assert _by_code(report, "balances_numeric")["status"] == "fail"
    assert report["overall"] == "fail"


# ----- per-type simulations -----

def test_bereavement_and_jury_are_no_impact():
    report = _report()
    assert _by_code(report, "sim_bereavement")["status"] == "pass"
    assert _by_code(report, "sim_bereavement")["projected"] is None
    assert _by_code(report, "sim_jury_duty")["projected"] is None


def test_carryover_declined_is_pass_not_a_problem():
    # No vacation -> a carryover/payout would be declined. That is a valid outcome,
    # not a setup problem, so the simulation stays 'pass' (surfaced only in the
    # preview) and does not drag the overall verdict down.
    fields = {**GOOD_FIELDS, "CurrentVacationBalance": 0}
    report = _report(employee_fields=fields)
    assert _by_code(report, "sim_carry_over")["status"] == "pass"
    assert _by_code(report, "sim_payout")["status"] == "pass"
    assert report["overall"] == "pass"


def test_current_balances_included():
    report = _report()
    assert report["current_balances"]["CurrentVacationBalance"] == 10.0
    assert set(report["current_balances"]) == {
        "CurrentVacationBalance", "CurrentSickDayBalance",
        "CurrentOvertimeBalance", "CarryOver", "Payout",
    }


# ----- SAFETY: no side effects -----

FORBIDDEN_CALLS = {
    "create_list_item",
    "update_list_item_fields",
    "send_email",
    "send_email_with_dashboard",
    "send_approval_email",
    "send_sms",
}


def _called_function_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_module_makes_no_write_or_notify_calls():
    source = inspect.getsource(ev)
    called = _called_function_names(source)
    leaked = called & FORBIDDEN_CALLS
    assert not leaked, f"validation module must not call writers/notifiers: {leaked}"
