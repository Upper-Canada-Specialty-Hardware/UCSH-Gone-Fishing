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

from app.config import settings
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
    found = await ev.fetch_approval_email_records(matched)

    assert found == {(list_id, emailed_id)}


def test_approval_email_records_is_empty_for_no_requests():
    assert asyncio.run(ev.fetch_approval_email_records([])) == set()


# ----- the whole-directory sweep -----
#
# The per-employee check is on-demand, so a record is only ever looked at after
# someone has been blocked by it. The sweep grades every record at once, which
# only helps if it flags exactly what would stall a request and nothing else -
# and if it stays at three SharePoint reads however many employees there are.

CURRENT_YEAR = date.today().year
SWEEP_HOLIDAYS = [
    {"id": "1", "fields": {"Title": "Canada Day", "Date": f"{CURRENT_YEAR}-07-01", "Province": "ON"}},
]


def _staff_record(item_id, name, email, manager_name, **field_overrides):
    """A healthy Staff Directory row, before any override breaks one thing."""
    fields = {
        "Title": name,
        "EmailAddress": email,
        "Location": "Barrie",               # maps to ON
        "Department": "Warehouse",
        "CurrentVacationBalance": 10,
        "CurrentSickDayBalance": 5,
        "CurrentOvertimeBalance": 2,
        "CarryOver": 0,
        "Payout": 0,
        "DefaultYearlyVacationDays": 15,
        "SickDayEntitlement": 5,
        "AllManagers": (
            [{"LookupId": 900, "LookupValue": manager_name}] if manager_name else []
        ),
    }
    fields.update(field_overrides)
    return {"id": str(item_id), "fields": fields}


def _uil(*people):
    """User Information List rows: (lookup id, display name, email) each."""
    return [
        {"id": str(lookup_id), "fields": {"Title": name, "EMail": email}}
        for lookup_id, name, email in people
    ]


# Two records that list each other as supervisor: everything resolves, nobody
# supervises themselves, so a clean sweep over them flags nothing at all.
ALICE = _staff_record(1, "Alice Worker", "alice@ucsh.ca", "Boss Person")
BOSS = _staff_record(2, "Boss Person", "boss@ucsh.ca", "Alice Worker")
PAIR_UIL = _uil(
    (101, "Alice Worker", "alice@ucsh.ca"),
    (102, "Boss Person", "boss@ucsh.ca"),
)


class _Reads:
    """Stands in for the three list reads, counting each one.

    The read count is the point of the sweep, so it is measured rather than
    assumed: one Staff Directory read, one Microsoft 365 directory read and one
    holiday read, whatever the headcount.
    """

    def __init__(self, staff, uil, holidays, uil_raises=False):
        self.staff = staff
        self.uil = uil
        self.holidays = holidays
        self.uil_raises = uil_raises
        self.staff_reads = 0
        self.uil_reads = 0
        self.holiday_reads = 0

    async def get_list_items(self, list_id, top=None):
        if list_id == "User Information List":
            self.uil_reads += 1
            if self.uil_raises:
                raise RuntimeError("Graph is down")
            return self.uil
        if list_id == settings.SP_LIST_STAFF_DIRECTORY:
            self.staff_reads += 1
            return self.staff
        raise AssertionError(f"the sweep read an unexpected list: {list_id}")

    async def get_all(self):
        self.holiday_reads += 1
        return self.holidays


def _sweep(monkeypatch, staff, uil=PAIR_UIL, holidays=None, uil_raises=False):
    """Run the sweep over a mocked directory. Returns (result, reads)."""
    reads = _Reads(staff, uil, SWEEP_HOLIDAYS if holidays is None else holidays, uil_raises)
    monkeypatch.setattr(ev.sp_client, "get_list_items", reads.get_list_items)
    monkeypatch.setattr(ev, "get_holiday_repository", lambda: reads)
    return asyncio.run(ev.validate_all_employee_setups()), reads


def _row(result, employee_id):
    return next(
        (r for r in result["flagged"] if r["employee_id"] == str(employee_id)), None,
    )


def _codes(row, key="fails"):
    return {p["code"] for p in row[key]}


def test_a_record_with_no_supervisor_is_flagged(monkeypatch):
    # The incident this exists for: an empty AllManagers column, and three leave
    # requests stalled on "cannot assign manager" before anyone noticed.
    no_supervisor = _staff_record(1, "Alice Worker", "alice@ucsh.ca", None)
    result, _ = _sweep(monkeypatch, [no_supervisor, BOSS])

    flagged = _row(result, 1)
    assert flagged is not None
    assert "supervisor_set" in _codes(flagged)
    assert flagged["employee_name"] == "Alice Worker"
    assert flagged["department"] == "Warehouse"


def test_a_record_with_no_email_is_flagged_on_identity(monkeypatch):
    no_email = _staff_record(1, "Alice Worker", "", "Boss Person")
    result, _ = _sweep(monkeypatch, [no_email, BOSS])

    assert "identity_roundtrip" in _codes(_row(result, 1))


def test_an_unrecognised_location_is_flagged(monkeypatch):
    on_mars = _staff_record(1, "Alice Worker", "alice@ucsh.ca", "Boss Person", Location="Mars")
    result, _ = _sweep(monkeypatch, [on_mars, BOSS])

    row = _row(result, 1)
    assert "location_province" in _codes(row)
    assert row["location"] == "Mars"


def test_a_fully_valid_directory_flags_nobody(monkeypatch):
    result, _ = _sweep(monkeypatch, [ALICE, BOSS])

    assert result["flagged"] == []
    assert result["total_checked"] == 2
    assert result["directory_unreadable"] is False


def test_a_warn_only_gap_does_not_flag_the_record(monkeypatch):
    # A payout above the cap and a missing entitlement are both worth a look,
    # neither stops a request - so the record stays off the list.
    warn_only = _staff_record(
        1, "Alice Worker", "alice@ucsh.ca", "Boss Person",
        Payout=7, DefaultYearlyVacationDays=0,
    )
    result, _ = _sweep(monkeypatch, [warn_only, BOSS])

    assert _row(result, 1) is None


def test_warns_ride_along_on_a_record_flagged_for_something_else(monkeypatch):
    # A supervisor with no email address cannot be sent an approval (a warn) and
    # matches no Microsoft 365 account (a fail). The fail is what lists the
    # record; the warn is shown beside it as context.
    silent_boss = _staff_record(2, "Boss Person", "", "Alice Worker")
    result, _ = _sweep(monkeypatch, [ALICE, silent_boss], uil=_uil((101, "Alice Worker", "alice@ucsh.ca")))

    row = _row(result, 1)
    assert "manager_m365_match" in _codes(row)
    assert "manager_reachable" in _codes(row, "warns")


def test_requests_and_simulations_are_never_reported(monkeypatch):
    # Those two sections grade requests, not the record, and belong to the
    # stuck-request view. A sweep row must only ever carry record-level checks.
    broken = _staff_record(1, "Alice Worker", "", None, Location="Mars", Payout=7)
    result, _ = _sweep(monkeypatch, [broken, BOSS])

    categories = {
        problem["category"]
        for row in result["flagged"]
        for key in ("fails", "warns")
        for problem in row[key]
    }
    assert categories
    assert categories <= {"identity", "supervisor", "location", "balances"}


def test_a_duplicate_name_flags_the_record_the_lookup_does_not_reach(monkeypatch):
    # A submitted request is matched back by display name, and the first record
    # carrying it always wins - so the second one is the one whose requests land
    # on somebody else.
    first = _staff_record(1, "Twin Name", "twin1@ucsh.ca", "Boss Person")
    second = _staff_record(2, "Twin Name", "twin2@ucsh.ca", "Boss Person")
    boss = _staff_record(3, "Boss Person", "boss@ucsh.ca", "Twin Name")
    uil = _uil(
        (101, "Twin Name", "twin1@ucsh.ca"),
        (102, "Twin Name", "twin2@ucsh.ca"),
        (103, "Boss Person", "boss@ucsh.ca"),
    )

    result, _ = _sweep(monkeypatch, [first, second, boss], uil=uil)

    second_row = _row(result, 2)
    assert _codes(second_row) >= {"identity_roundtrip", "identity_unique_name"}
    # It has to name the record the lookup actually reaches, or nobody can tell
    # the two apart.
    identity = next(p for p in second_row["fails"] if p["code"] == "identity_roundtrip")
    assert "#1" in identity["detail"]

    # The first record still resolves to itself; only the shared name is wrong.
    first_row = _row(result, 1)
    assert _codes(first_row) == {"identity_unique_name"}


def test_an_unreadable_directory_does_not_flag_everyone_on_identity(monkeypatch):
    # Reporting zero Microsoft 365 accounts after a failed read would accuse
    # every correctly configured record at once.
    no_supervisor = _staff_record(1, "Alice Worker", "alice@ucsh.ca", None)
    result, _ = _sweep(monkeypatch, [no_supervisor, BOSS], uil_raises=True)

    assert result["directory_unreadable"] is True
    assert all(
        "identity_roundtrip" not in _codes(row) for row in result["flagged"]
    )
    # The checks that do not depend on the directory still report.
    assert "supervisor_set" in _codes(_row(result, 1))


def test_the_read_count_does_not_grow_with_headcount(monkeypatch):
    staff = [
        _staff_record(1, "Alice Worker", "alice@ucsh.ca", "Boss Person"),
        _staff_record(2, "Boss Person", "boss@ucsh.ca", "Alice Worker"),
        _staff_record(3, "Carl Third", "carl@ucsh.ca", "Boss Person"),
    ]
    uil = _uil(
        (101, "Alice Worker", "alice@ucsh.ca"),
        (102, "Boss Person", "boss@ucsh.ca"),
        (103, "Carl Third", "carl@ucsh.ca"),
    )

    result, reads = _sweep(monkeypatch, staff, uil=uil)

    assert result["total_checked"] == 3
    assert (reads.staff_reads, reads.uil_reads, reads.holiday_reads) == (1, 1, 1)


def test_flagged_records_are_sorted_by_name(monkeypatch):
    staff = [
        _staff_record(1, "zoe last", "zoe@ucsh.ca", None),
        _staff_record(2, "Alan First", "alan@ucsh.ca", None),
    ]
    result, _ = _sweep(monkeypatch, staff, uil=_uil(
        (101, "zoe last", "zoe@ucsh.ca"), (102, "Alan First", "alan@ucsh.ca"),
    ))

    assert [row["employee_name"] for row in result["flagged"]] == ["Alan First", "zoe last"]


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
