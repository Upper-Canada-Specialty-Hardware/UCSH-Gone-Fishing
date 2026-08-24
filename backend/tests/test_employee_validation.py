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
import asyncio
import inspect
import uuid
from datetime import date, timedelta

from app.services import employee_validation as ev
from app.services.employee_validation import build_validation_report, summarise_request


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
    # A healthy record carries its yearly grants; without them the entitlement
    # measurement has nothing to work from.
    "DefaultYearlyVacationDays": 15,
    "SickDayEntitlement": 5,
}

MANAGERS = [{"id": "5", "fields": {"Title": "Boss", "EmailAddress": "boss@ucsh.ca"}}]
# Dated in the year the sample range falls in, so the current-year check is met.
HOLIDAYS = [{"Title": "Canada Day", "Date": "2026-07-01", "Province": "ON"}]
PASS_IDENTITY = {"status": "pass", "detail": "round-trip ok", "account_count": 1}
SAMPLE = (date(2026, 7, 6), date(2026, 7, 7))  # a Monday..Tuesday
TODAY = date(2026, 7, 6)  # fixed, so the automatic-rejection window is not clock-dependent


def _request(kind="leave", **overrides):
    """A request summary in the shape summarise_request produces.

    `notified` defaults to True because that is what a healthy pending request
    looks like: it has a row in the approval-state table, written when its
    approval email was composed.
    """
    summary = {
        "kind": kind,
        "item_id": "12",
        "status": "Pending",
        "has_amount": True,
        "has_manager": True,
        "notified": True,
        "created_date": TODAY.isoformat(),
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
        "auto_rejected": False,
        "auto_reject_reason": None,
    }
    summary.update(overrides)
    return summary


def _report(**overrides):
    kwargs = dict(
        employee_id="1",
        employee_name="Test Employee",
        employee_fields=GOOD_FIELDS,
        managers=MANAGERS,
        all_managers_count=1,
        manager_m365_matches=1,
        same_name_others=0,
        identity=PASS_IDENTITY,
        province="ON",
        province_error=None,
        holidays=HOLIDAYS,
        requests=[],
        sample_start=SAMPLE[0],
        sample_end=SAMPLE[1],
        today=TODAY,
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
    # No address means no Microsoft 365 match either, so both rows react.
    report = _report(
        managers=[{"id": "5", "fields": {"Title": "Boss", "EmailAddress": ""}}],
        manager_m365_matches=0,
    )
    assert _by_code(report, "manager_reachable")["status"] == "warn"
    assert _by_code(report, "manager_m365_match")["status"] == "fail"


def test_supervisor_email_that_does_not_resolve_fails():
    # The address is filled in, which is all the old check looked at — but it
    # matches no Microsoft 365 account, so no manager can be recorded against
    # their requests and nobody is ever notified.
    report = _report(manager_m365_matches=0)
    assert _by_code(report, "manager_reachable")["status"] == "pass"
    m365 = _by_code(report, "manager_m365_match")
    assert m365["status"] == "fail"
    assert m365["measure"]["actual"] == 0
    assert m365["measure"]["expected"] == 1
    assert report["overall"] == "fail"


def test_employee_listed_as_their_own_supervisor_fails():
    fields = {**GOOD_FIELDS, "AllManagers": [{"LookupId": 1, "LookupValue": "Test Employee"}]}
    report = _report(employee_fields=fields)
    assert _by_code(report, "supervisor_not_self")["status"] == "fail"


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


# ----- identity -----

def test_shared_display_name_fails():
    # Requests are matched back to a person by display name, so a duplicate can
    # attach someone's request to the wrong record.
    report = _report(same_name_others=1)
    assert _by_code(report, "identity_unique_name")["status"] == "fail"


# ----- calendar -----

def test_calendar_with_no_current_year_rows_warns():
    stale = [{"Title": "Canada Day", "Date": "2019-07-01", "Province": "ON"}]
    report = _report(holidays=stale)
    assert _by_code(report, "holidays_load")["status"] == "pass"       # rows did load
    assert _by_code(report, "holidays_current_year")["status"] == "warn"


# ----- balances -----

def test_payout_above_the_cap_warns():
    # Approval refuses a payout that would push the total past five days, so a
    # stored value above it means something only half-applied.
    fields = {**GOOD_FIELDS, "Payout": 7}
    report = _report(employee_fields=fields)
    assert _by_code(report, "balances_in_range")["status"] == "warn"


def test_negative_vacation_is_not_graded():
    # Vacation is the pot the cascade overflows into and is allowed to go
    # negative — it is reported, never graded.
    fields = {**GOOD_FIELDS, "CurrentVacationBalance": -3}
    report = _report(employee_fields=fields)
    assert _by_code(report, "balances_in_range")["status"] == "pass"


def test_missing_entitlements_warn():
    fields = {**GOOD_FIELDS, "DefaultYearlyVacationDays": 0, "SickDayEntitlement": 0}
    report = _report(employee_fields=fields)
    entitlements = _by_code(report, "entitlements_set")
    assert entitlements["status"] == "warn"
    assert entitlements["measure"]["actual"] == 0


# ----- simulations measure the arithmetic, not just "it ran" -----

def test_leave_simulation_measures_days_deducted():
    report = _report()
    measure = _by_code(report, "sim_vacation")["measure"]
    # One day taken must come out of the pots exactly once, wherever the
    # cascade takes it from.
    assert measure["actual"] == 1.0
    assert measure["expected"] == 1.0


def test_overtime_simulation_measures_days_credited():
    report = _report()
    measure = _by_code(report, "sim_overtime")["measure"]
    assert measure["actual"] == 1.0    # eight hours is one make-up day
    assert measure["expected"] == 1.0


def test_carryover_simulation_measures_both_sides_of_the_transfer():
    report = _report()
    measure = _by_code(report, "sim_carry_over")["measure"]
    # Vacation must fall by the same amount carry-over rises by.
    assert measure["actual"] == 2
    assert measure["expected"] == 2


def test_no_cost_leave_types_measure_zero():
    report = _report()
    assert _by_code(report, "sim_bereavement")["measure"]["actual"] == 0.0
    assert _by_code(report, "sim_jury_duty")["measure"]["actual"] == 0.0


def test_business_day_calculation_is_measured_against_the_calendar():
    report = _report()
    measure = _by_code(report, "sim_business_days")["measure"]
    assert measure["actual"] == 2      # Monday and Tuesday, no holiday between
    assert measure["expected"] == 2


# ----- requests already in flight -----

def test_no_requests_is_clean():
    report = _report()
    assert _by_code(report, "requests_missing_days")["status"] == "pass"
    assert _by_code(report, "requests_missing_manager")["status"] == "pass"
    assert _by_code(report, "requests_auto_rejected")["status"] == "pass"
    assert report["overall"] == "pass"


def test_stalled_request_fails_the_whole_check():
    # This is the case that reported green while the employee was blocked: a
    # pending request that never got its days or its manager, invisible on every
    # dashboard.
    report = _report(requests=[_request(has_amount=False, has_manager=False, notified=False)])
    assert _by_code(report, "requests_missing_days")["status"] == "fail"
    assert _by_code(report, "requests_missing_manager")["status"] == "fail"
    assert report["overall"] == "fail"
    # The detail has to name the request, otherwise nobody can go and clear it.
    assert "#12" in _by_code(report, "requests_missing_manager")["detail"]


def test_request_whose_manager_was_never_asked_fails():
    report = _report(requests=[_request(notified=False)])
    assert _by_code(report, "requests_not_notified")["status"] == "fail"


def test_automatic_rejection_warns_and_quotes_the_reason():
    reason = "This request overlaps with leave request #11 covering 2026-09-01 to 2026-09-05."
    report = _report(requests=[
        _request(status="Rejected", auto_rejected=True, auto_reject_reason=reason),
    ])
    check = _by_code(report, "requests_auto_rejected")
    assert check["status"] == "warn"
    # Quoting it verbatim is the point: it names what blocked them.
    assert "#11" in check["detail"]


def test_approved_requests_are_reported_not_graded():
    report = _report(requests=[_request(status="Approved")])
    check = _by_code(report, "requests_approved_dates")
    assert check["status"] == "pass"
    assert check["measure"]["actual"] == 1
    assert check["measure"]["comparison"] == "reported"


# ----- the report is countable -----

def test_measurements_are_tallied():
    report = _report()
    tally = report["measurements"]
    assert tally["total"] > 0
    assert tally["within_range"] == tally["total"]      # a healthy record


def test_failing_measurement_lowers_the_tally():
    report = _report(manager_m365_matches=0)
    tally = report["measurements"]
    assert tally["within_range"] == tally["total"] - 1


def test_reported_only_rows_are_left_out_of_the_tally():
    # An approved request carries a number but no expectation, so it must not be
    # counted as an automatic pass.
    with_approved = _report(requests=[_request(status="Approved")])
    without = _report()
    assert with_approved["measurements"]["total"] == without["measurements"]["total"]


# ----- summarising a raw request row -----

def test_summarise_leave_request_reads_the_processing_state():
    item = {
        "id": "12",
        "fields": {
            "Status": "Pending", "Days": 0, "Title": "Someone /// notes",
            "StartDate": "2026-09-01T00:00:00Z", "EndDate": "2026-09-05T00:00:00Z",
        },
    }
    summary = summarise_request("leave", item)
    assert summary["has_amount"] is False       # zero days is not calculated
    assert summary["has_manager"] is False
    # Not passed in, so it stays unknown — never guessed at from the item.
    assert summary["notified"] is None
    assert summary["start_date"] == "2026-09-01"
    assert summary["auto_rejected"] is False


def test_summarise_extracts_the_automatic_rejection_reason():
    item = {
        "id": "13",
        "fields": {
            "Status": "Rejected",
            "Title": "Someone [Auto-Rejected: This overlaps leave request #11.]",
        },
    }
    summary = summarise_request("leave", item)
    assert summary["auto_rejected"] is True
    assert "#11" in summary["auto_reject_reason"]


def test_summarise_overtime_reads_hours_and_takes_its_notified_state():
    item = {"id": "21", "fields": {"Status": "Pending", "Hours": 8, "ManagerLookupId": 5}}
    summary = summarise_request("overtime", item, notified=True)
    assert summary["has_amount"] is True
    assert summary["has_manager"] is True
    # Overtime is graded on this too now: the approval-state table records the
    # send for all three request types, not just leave.
    assert summary["notified"] is True


# ----- the seam between a raw row and the report -----
#
# The two halves used to be tested apart: the report against a hand-written
# summary, the summariser against a raw row. That let "was the manager asked?"
# be answered from ApproveProcessedFlag, which is set only when a decision is
# applied — so every healthy pending request was reported as never sent, and no
# test carried a realistic row far enough to notice. These run a raw row all the
# way through.

HEALTHY_PENDING_LEAVE = {
    "id": "12",
    "fields": {
        "Status": "Pending",
        "Days": 3,
        "ManagerLookupId": 5,
        # Set at creation and only flipped to "Processed" once a decision is
        # applied, so on a live pending request it always reads like this.
        "ApproveProcessedFlag": "Not Processed",
        "StartDate": "2026-09-01T00:00:00Z",
        "EndDate": "2026-09-03T00:00:00Z",
    },
    "createdDateTime": "2026-07-01T09:00:00Z",
}


def test_a_healthy_pending_request_does_not_fail_the_check():
    # The regression this file exists to prevent: a live request awaiting its
    # manager is normal, not a setup problem.
    summary = summarise_request("leave", HEALTHY_PENDING_LEAVE, notified=True)
    report = _report(requests=[summary])

    assert _by_code(report, "requests_not_notified")["status"] == "pass"
    assert _by_code(report, "requests_missing_days")["status"] == "pass"
    assert _by_code(report, "requests_missing_manager")["status"] == "pass"
    assert report["overall"] == "pass"


def test_notified_is_never_read_off_the_sharepoint_item():
    # Whichever way ApproveProcessedFlag reads, the answer comes from the caller.
    sent = summarise_request("leave", HEALTHY_PENDING_LEAVE, notified=True)
    assert sent["notified"] is True

    decided = {"id": "13", "fields": {"Status": "Approved", "ApproveProcessedFlag": "Processed"}}
    assert summarise_request("leave", decided, notified=False)["notified"] is False


def test_unreadable_send_record_warns_rather_than_accusing():
    # None means "could not look", which must not be reported as "never sent".
    report = _report(requests=[_request(notified=None)])
    row = _by_code(report, "requests_not_notified")

    assert row["status"] == "warn"
    assert row["measure"]["comparison"] == "reported"   # kept out of the tally
    assert report["overall"] == "warn"


def test_a_proven_unsent_request_still_fails_alongside_an_unknown_one():
    # One unknown must not mask a request that is genuinely stuck.
    report = _report(requests=[_request(notified=None), _request(item_id="99", notified=False)])
    assert _by_code(report, "requests_not_notified")["status"] == "fail"


# ----- automatic rejections age out -----

def test_a_recent_automatic_rejection_warns():
    recent = _request(
        status="Rejected", auto_rejected=True, auto_reject_reason="overlaps #11",
        created_date=(TODAY - timedelta(days=10)).isoformat(),
    )
    assert _by_code(_report(requests=[recent]), "requests_auto_rejected")["status"] == "warn"


def test_an_old_automatic_rejection_is_not_graded():
    # Otherwise every employee who ever hit one shows amber for good.
    old = _request(
        status="Rejected", auto_rejected=True, auto_reject_reason="overlaps #11",
        created_date=(TODAY - timedelta(days=400)).isoformat(),
    )
    report = _report(requests=[old])
    assert _by_code(report, "requests_auto_rejected")["status"] == "pass"
    assert report["overall"] == "pass"


def test_an_automatic_rejection_with_no_creation_date_is_kept():
    # Nothing should vanish because a field was empty.
    unknown_age = _request(
        status="Rejected", auto_rejected=True, auto_reject_reason="overlaps #11",
        created_date="",
    )
    assert _by_code(_report(requests=[unknown_age]), "requests_auto_rejected")["status"] == "warn"


# ----- a failed read is not a failed measurement -----

def test_unreadable_m365_directory_warns_rather_than_failing_the_supervisor():
    report = _report(manager_m365_matches=None)
    row = _by_code(report, "manager_m365_match")

    assert row["status"] == "warn"
    assert row["measure"]["comparison"] == "reported"
    assert report["overall"] == "warn"


def test_unreadable_staff_directory_warns_rather_than_claiming_a_unique_name():
    report = _report(same_name_others=None)
    row = _by_code(report, "identity_unique_name")

    assert row["status"] == "warn"
    assert row["measure"]["comparison"] == "reported"


def test_rows_that_could_not_be_measured_are_left_out_of_the_tally():
    graded = _report()
    ungraded = _report(manager_m365_matches=None)
    assert ungraded["measurements"]["total"] == graded["measurements"]["total"] - 1


# ----- the send record is read from the database, not guessed -----
#
# This is the query the whole "was the manager asked?" check rests on, so it is
# exercised against a real session rather than trusted.

def test_approval_email_records_returns_only_the_requests_that_have_a_row():
    asyncio.run(_approval_record_flow())


async def _approval_record_flow():
    from app.database import Base, async_session, engine
    from app.models import RequestApprovalState
    from app.services.approval_versions import bump_and_snapshot

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    list_id = "list-for-setup-check"
    emailed_id = f"sent-{uuid.uuid4().hex}"
    silent_id = f"unsent-{uuid.uuid4().hex}"

    # Composing an approval email is what writes the row; nothing else does.
    await bump_and_snapshot(list_id, emailed_id, {"Days": 1}, ("Days",))
    async with async_session() as session:
        assert await session.get(RequestApprovalState, (list_id, emailed_id)) is not None

    matched = [
        ("leave", list_id, {"id": emailed_id}),
        ("leave", list_id, {"id": silent_id}),
    ]
    found = await ev._fetch_approval_email_records(matched)

    assert found == {(list_id, emailed_id)}


def test_approval_email_records_is_empty_for_no_requests():
    assert asyncio.run(ev._fetch_approval_email_records([])) == set()


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
