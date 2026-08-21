"""Read-only "does this employee's setup work?" validation suite (GH #41).

Runs an employee's CURRENT Staff Directory values through every request
workflow - identity resolution, supervisor/manager lookup, location -> province
-> holiday calendar, and a pure balance simulation for each leave / overtime /
carryover-payout type - WITHOUT creating any request or sending any
notification.

Why this exists: previously the only way to confirm an employee was set up
correctly (e.g. "is their supervisor linked?") was to fire a REAL leave request
in their name, which notified uninvolved employees. This suite reproduces every
check that a real request would exercise, using only reads and the balance
engine's pure `simulate_*` functions.

Two things shape how it reports.

Every check that can be counted carries a MEASUREMENT - what was measured, what
it was held against, and the comparison between them - and its verdict is
derived from that comparison rather than asserted alongside it. The balance
simulations previously reported a pass whenever nothing raised, which meant they
could not fail on a wrong figure; they now assert the arithmetic.

It also reads the employee's OWN REQUESTS, not just their Staff Directory
record. A request stalled part-way through processing is hidden from every
dashboard, so a record could measure perfectly while its owner was unable to get
a request through - which is exactly how a green report and a blocked employee
coexisted.

SAFETY CONTRACT: this module performs READS ONLY. It must never call
`create_list_item`, `update_list_item_fields`, `send_email`,
`send_email_with_dashboard`, `send_approval_email`, or `send_sms`. A guard test
(`tests/test_employee_validation.py`) enforces that it stays side-effect free -
keep it that way.

The suite is split like the balance engine: `build_validation_report(...)` is a
pure function over already-fetched inputs (trivially unit-testable), and
`validate_employee_setup(...)` is the thin async wrapper that does the SharePoint
reads and feeds the pure core.
"""

import logging
from datetime import date, timedelta

from app.config import settings
from app.graph.sharepoint import sp_client
from app.services.employee import (
    get_all_managers_for_employee,
    get_employee_by_id,
    map_location_to_province,
    resolve_person_field,
)
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.overlap_detection import _extract_lookup_id
from app.services.holidays import get_holidays_for_province, get_half_friday_season
from app.services.business_days import calculate_business_days
from app.services.balance import (
    simulate_leave_impact,
    simulate_overtime_impact,
    simulate_carryover_payout_impact,
)

logger = logging.getLogger(__name__)

# Representative sample inputs for the dry-run simulations. Nothing is written -
# these only drive the pure `simulate_*` math so the report can show "if this
# employee took X, here is what happens to their balances".
SAMPLE_LEAVE_DAYS = 1.0
SAMPLE_HALF_DAY = 0.5
SAMPLE_OVERTIME_HOURS = 8.0
SAMPLE_CO_PO_DAYS = 1.0

# The five balance "pots" on the Staff Directory record, by their SP column name.
BALANCE_POTS = [
    "CurrentVacationBalance",
    "CurrentSickDayBalance",
    "CurrentOvertimeBalance",
    "CarryOver",
    "Payout",
]

# Leave types run through simulate_leave_impact. (code, LeaveType, sample days)
LEAVE_TYPE_CASES = [
    ("sim_vacation", "Vacation", SAMPLE_LEAVE_DAYS),
    ("sim_sick", "Sick or Personal Day", SAMPLE_LEAVE_DAYS),
    ("sim_half_day", "Half Day or Partial Day Off", SAMPLE_HALF_DAY),
    ("sim_bereavement", "Bereavement", SAMPLE_LEAVE_DAYS),
    ("sim_jury_duty", "Jury Duty", SAMPLE_LEAVE_DAYS),
]

# Sane ranges for the balance pots, taken from the rules the approval path
# already enforces. Vacation is deliberately absent: it is the pot the cascade
# overflows into and is allowed to go negative, so it is reported, not graded.
BALANCE_BOUNDS = {
    "CurrentSickDayBalance": (0, None),
    "CurrentOvertimeBalance": (None, None),
    "CarryOver": (0, None),
    "Payout": (0, 5),  # approval refuses a payout that would push the total past 5
}

# The four pots leave spending moves between. The cascade passes a deficit down
# the chain rather than clamping it, so their total falls by exactly the days
# taken — which is the arithmetic the simulation checks assert.
LEAVE_POTS = [
    "CurrentVacationBalance",
    "CurrentSickDayBalance",
    "CurrentOvertimeBalance",
    "CarryOver",
]

# Written into the Title by every automatic rejection; see auto_reject_titles.
AUTO_REJECT_MARKER = "[Auto-Rejected:"


def _check(code: str, category: str, status: str, detail: str, projected=None, measure=None) -> dict:
    """One row of the report. status is 'pass' | 'warn' | 'fail'.

    Args:
        code: Stable identifier the dashboard keys its wording off.
        category: Grouping shown in the technical breakdown.
        status: 'pass', 'warn' or 'fail'.
        detail: Sentence explaining the outcome in plain language.
        projected: Optional balance preview for the simulation rows.
        measure: Optional {"label", "actual", "expected", "comparison"} record.
            Rows carrying one are countable, which is what lets the report say
            how many measurements landed in range rather than only showing a
            colour.

    Returns:
        One report row.
    """
    return {
        "code": code,
        "category": category,
        "status": status,
        "detail": detail,
        "projected": projected,
        "measure": measure,
    }


def _compare(actual, expected, comparison: str) -> bool:
    """Apply one named comparison between a measured and an expected value.

    Args:
        actual: The measured value.
        expected: The value it is held against.
        comparison: 'equals', 'at_least', 'at_most' or 'within'
            ('within' expects a (low, high) pair).

    Returns:
        True when the measurement is in range.
    """
    if comparison == "equals":
        return actual == expected
    if comparison == "at_least":
        return actual >= expected
    if comparison == "at_most":
        return actual <= expected
    if comparison == "within":
        low, high = expected
        return low <= actual <= high
    raise ValueError(f"Unknown comparison: {comparison}")


def _measured(
    code: str, category: str, *, label: str, actual, expected, comparison: str,
    ok_detail: str, bad_detail: str, bad_status: str = "fail", projected=None,
) -> dict:
    """Build a check whose verdict is derived from a measurement.

    Every check that can be counted goes through here, so the pass/fail wording
    and the number behind it can never drift apart.

    Args:
        code: Stable check identifier.
        category: Grouping shown in the technical breakdown.
        label: Human name for what was measured.
        actual: The measured value.
        expected: The value it must satisfy.
        comparison: One of the comparisons `_compare` understands.
        ok_detail: Sentence used when the measurement is in range.
        bad_detail: Sentence used when it is not.
        bad_status: 'fail' (blocks requests) or 'warn' (worth a look).
        projected: Optional balance preview, for the simulation rows.

    Returns:
        One report row carrying its measurement.
    """
    in_range = _compare(actual, expected, comparison)  # verdict is derived, never asserted
    return _check(
        code, category,
        "pass" if in_range else bad_status,
        ok_detail if in_range else bad_detail,
        projected=projected,
        measure={
            "label": label,
            "actual": actual,
            "expected": list(expected) if comparison == "within" else expected,
            "comparison": comparison,
        },
    )


def _count_all_managers(fields: dict) -> int:
    am = fields.get("AllManagers")
    return len(am) if isinstance(am, list) else 0


def _sample_weekday_range() -> tuple[date, date]:
    """Next Monday..Tuesday - a stable 1-business-day range for the calc check."""
    today = date.today()
    # weekday(): Mon=0 .. Sun=6. Days until next Monday (at least 1 day out).
    days_ahead = (7 - today.weekday()) % 7 or 7
    start = today + timedelta(days=days_ahead)
    return start, start + timedelta(days=1)


def _overall(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def build_validation_report(
    *,
    employee_id: str,
    employee_name: str,
    employee_fields: dict,
    managers: list[dict],
    all_managers_count: int,
    manager_m365_matches: int,
    same_name_others: int,
    identity: dict,
    province: str | None,
    province_error: str | None,
    holidays: list[dict],
    requests: list[dict],
    sample_start: date,
    sample_end: date,
) -> dict:
    """Pure core: assemble the full report from already-fetched inputs.

    Args:
        employee_id: Staff Directory item id.
        employee_name: Their display name, as the directory holds it.
        employee_fields: Their Staff Directory field values.
        managers: Supervisors already resolved via get_all_managers_for_employee.
        all_managers_count: How many supervisors the record lists, resolved or not.
        manager_m365_matches: How many of those supervisors have an email that
            resolves to a Microsoft 365 account — the value the request path
            needs before it can record a manager against a request.
        same_name_others: Other staff sharing this display name.
        identity: Pre-computed round-trip result {"status", "detail", "account_count"}.
        province: Province mapped from their location, or None.
        province_error: Why the mapping failed, when it did.
        holidays: Holiday rows already fetched for that province.
        requests: Their existing requests, summarised by `summarise_request`.
        sample_start: First day of the sample range used for the day calculation.
        sample_end: Last day of that range.

    Returns:
        The full report: overall verdict, measurement tally, current balances
        and every check row.
    """
    checks: list[dict] = []

    # --- Identity (email -> M365 user -> Staff Directory round-trip) ---
    # The round-trip already carries its own verdict; the count behind it is
    # attached so this row is countable like the rest.
    checks.append(_check(
        "identity_roundtrip", "identity", identity["status"], identity["detail"],
        measure={
            "label": "Microsoft 365 accounts matching their email",
            "actual": identity.get("account_count", 0),
            "expected": 1,
            "comparison": "equals",
        },
    ))

    # A submitted request is matched back to a person through their display
    # name, so a name shared with someone else routes to whichever record is
    # found first.
    checks.append(_measured(
        "identity_unique_name", "identity",
        label="Other staff sharing this display name",
        actual=same_name_others, expected=0, comparison="equals",
        ok_detail="Their name is unique in the Staff Directory.",
        bad_detail=(
            f"{same_name_others} other staff record(s) are also named "
            f"'{employee_name}'. Requests are matched back to a person by name, "
            "so theirs can be attached to the wrong record."
        ),
    ))

    # --- Supervisor / AllManagers ---
    checks.append(_measured(
        "supervisor_set", "supervisor",
        label="Supervisors listed",
        actual=all_managers_count, expected=1, comparison="at_least",
        ok_detail=f"{all_managers_count} supervisor(s) listed.",
        bad_detail=(
            "No supervisor is set on the Staff Directory record, so a request "
            "would have no one to approve it."
        ),
    ))

    if all_managers_count:
        # Every listed supervisor must resolve to a real Staff Directory record
        # via the SAME path the approval email uses.
        checks.append(_measured(
            "supervisor_resolves", "supervisor",
            label="Listed supervisors matching a real staff record",
            actual=len(managers), expected=all_managers_count, comparison="equals",
            ok_detail=(
                "All listed supervisors match a real employee: "
                + ", ".join(m.get("fields", {}).get("Title", "?") for m in managers)
            ),
            bad_detail=(
                f"Only {len(managers)} of {all_managers_count} listed supervisor(s) "
                "match a real employee, so approval routing would be incomplete."
            ),
        ))

        # Each resolved supervisor needs an email to receive the approval at all.
        unreachable = [
            m.get("fields", {}).get("Title", "?")
            for m in managers
            if not (m.get("fields", {}).get("EmailAddress") or "").strip()
        ]
        checks.append(_measured(
            "manager_reachable", "supervisor",
            label="Supervisors with an email address",
            actual=len(managers) - len(unreachable), expected=len(managers),
            comparison="equals",
            ok_detail="All supervisors have an email address.",
            bad_detail=(
                "Supervisor(s) with no email address, so approval emails can't "
                "reach them: " + ", ".join(unreachable)
            ),
            bad_status="warn",
        ))

        # Having an address is not the same as that address resolving. Recording
        # a manager against a request needs the email to match a Microsoft 365
        # account; when it doesn't, the request keeps no manager, notifies
        # nobody and stays hidden from the dashboards — while this check used to
        # report green on the strength of a non-empty string.
        checks.append(_measured(
            "manager_m365_match", "supervisor",
            label="Supervisor emails matching a Microsoft 365 account",
            actual=manager_m365_matches, expected=len(managers), comparison="equals",
            ok_detail="Every supervisor's email matches a Microsoft 365 account.",
            bad_detail=(
                f"Only {manager_m365_matches} of {len(managers)} supervisor "
                "email(s) match a Microsoft 365 account. Their requests cannot "
                "record a manager, so nobody is notified and the request stays "
                "hidden from the dashboards."
            ),
        ))

        # A record listing itself as its own supervisor routes approvals back to
        # the person who submitted them.
        self_supervising = sum(
            1 for entry in (employee_fields.get("AllManagers") or [])
            if isinstance(entry, dict) and entry.get("LookupValue") == employee_name
        )
        checks.append(_measured(
            "supervisor_not_self", "supervisor",
            label="Supervisor entries naming the employee themselves",
            actual=self_supervising, expected=0, comparison="equals",
            ok_detail="No supervisor entry points back at this employee.",
            bad_detail=(
                "This employee is listed as their own supervisor, so their "
                "requests would be sent to them to approve."
            ),
        ))

    # --- Location -> province ---
    checks.append(_measured(
        "location_province", "location",
        label="Locations mapping to a province",
        actual=0 if province_error else 1, expected=1, comparison="equals",
        ok_detail=f"Location '{employee_fields.get('Location', '')}' maps to province {province}.",
        bad_detail=(
            f"{province_error}. Leave days cannot be calculated until a valid "
            "location is set, so the request would get stuck."
        ),
    ))

    # --- Holiday calendar for the province ---
    if province and not province_error:
        season = get_half_friday_season(holidays)
        season_note = (
            " Half-Friday season detected." if season[0] and season[1]
            else " (No half-Friday season rows found for this province.)"
        )
        checks.append(_measured(
            "holidays_load", "location",
            label=f"Holiday rows loaded for {province}",
            actual=len(holidays), expected=1, comparison="at_least",
            ok_detail=f"{len(holidays)} holiday row(s) loaded for {province}.{season_note}",
            bad_detail=(
                f"No holidays are set for province {province}, so every weekday "
                "will count as a workday when leave is calculated."
            ),
            bad_status="warn",
        ))

        # A calendar holding only past years still loads, and still silently
        # miscounts every leave request taken this year.
        this_year = date.today().year
        current_year_rows = sum(
            1 for h in holidays
            if str(h.get("Date", ""))[:4] == str(this_year)
        )
        checks.append(_measured(
            "holidays_current_year", "location",
            label=f"Holiday rows dated {this_year}",
            actual=current_year_rows, expected=1, comparison="at_least",
            ok_detail=f"{current_year_rows} holiday row(s) cover {this_year}.",
            bad_detail=(
                f"The {province} calendar has no holidays dated {this_year}, so "
                "this year's holidays will be counted as ordinary workdays."
            ),
            bad_status="warn",
        ))

    # --- Balance pots are numeric ---
    bad_pots = []
    for pot in BALANCE_POTS:
        raw = employee_fields.get(pot)
        try:
            float(raw or 0)
        except (TypeError, ValueError):
            bad_pots.append(f"{pot}={raw!r}")
    checks.append(_measured(
        "balances_numeric", "balances",
        label="Balance figures that parse as a number",
        actual=len(BALANCE_POTS) - len(bad_pots), expected=len(BALANCE_POTS),
        comparison="equals",
        ok_detail=(
            "All five balance pots are numeric: "
            + ", ".join(f"{p}={_as_float(employee_fields.get(p))}" for p in BALANCE_POTS)
        ),
        bad_detail=(
            "These balance values are not numbers: " + ", ".join(bad_pots)
            + ". Leave calculations cannot run until they are corrected."
        ),
    ))

    # --- Balance pots sit in a sane range ---
    # Only meaningful once they parse; a non-numeric pot is already a failure above.
    if not bad_pots:
        out_of_range = []
        for pot, (low, high) in BALANCE_BOUNDS.items():
            value = _as_float(employee_fields.get(pot))
            if low is not None and value < low:
                out_of_range.append(f"{pot}={value} (below {low})")
            elif high is not None and value > high:
                out_of_range.append(f"{pot}={value} (above {high})")
        checks.append(_measured(
            "balances_in_range", "balances",
            label="Balance figures outside their expected range",
            actual=len(out_of_range), expected=0, comparison="equals",
            ok_detail=(
                "Every graded balance sits in range. Vacation is not graded — it "
                "is the pot the cascade overflows into and may run negative."
            ),
            bad_detail=(
                "Balance value(s) outside the expected range: "
                + ", ".join(out_of_range)
                + ". These are usually a sign an approval only half-applied."
            ),
            bad_status="warn",
        ))

    # --- Yearly entitlements are set ---
    # These drive what an employee is granted; a zero here is almost always an
    # unfinished record rather than a deliberate value.
    missing_entitlements = [
        label for label, field in (
            ("yearly vacation", "DefaultYearlyVacationDays"),
            ("sick", "SickDayEntitlement"),
        )
        if not _as_float(employee_fields.get(field)) > 0
    ]
    checks.append(_measured(
        "entitlements_set", "balances",
        label="Yearly entitlements with a value above zero",
        actual=2 - len(missing_entitlements), expected=2, comparison="equals",
        ok_detail="Both the yearly vacation and sick entitlements are set.",
        bad_detail=(
            "No entitlement recorded for: " + ", ".join(missing_entitlements)
            + ". Their yearly grant cannot be worked out from this record."
        ),
        bad_status="warn",
    ))

    # --- Per-leave-type dry runs (current year) ---
    for code, leave_type, days in LEAVE_TYPE_CASES:
        checks.append(_simulate_leave_case(code, leave_type, employee_fields, days, is_next_year=False))

    # --- Next-year cascade branch (Vacation) ---
    checks.append(_simulate_leave_case(
        "sim_next_year_vacation", "Vacation", employee_fields, SAMPLE_LEAVE_DAYS, is_next_year=True,
    ))

    # --- Business-day calculation over the sample range ---
    if province and not province_error:
        # The sample range is next Monday to Tuesday: two working days, less any
        # holiday landing on either. No Friday in range, so the half-Friday rule
        # cannot come into it.
        holiday_dates = {str(h.get("Date", ""))[:10] for h in holidays}
        expected_working_days = 2 - sum(
            1 for d in (sample_start, sample_end) if d.isoformat() in holiday_dates
        )
        try:
            season = get_half_friday_season(holidays)
            counted = calculate_business_days(sample_start, sample_end, holidays, season)
            checks.append(_measured(
                "sim_business_days", "simulation",
                label=f"Working days counted from {sample_start} to {sample_end}",
                actual=counted, expected=expected_working_days, comparison="equals",
                ok_detail=f"Day calculation over {sample_start}..{sample_end} returns {counted}, as expected.",
                bad_detail=(
                    f"Day calculation over {sample_start}..{sample_end} returned "
                    f"{counted}, but the holiday calendar says it should be "
                    f"{expected_working_days}."
                ),
            ))
        except Exception as e:  # noqa: BLE001 - report any failure as a red check
            checks.append(_check(
                "sim_business_days", "simulation", "fail",
                f"Day calculation raised: {e}",
            ))

    # --- Overtime dry run ---
    # Approving overtime credits hours/8 to the make-up pot, after which the
    # vacation offset can move days between vacation and make-up. The pair total
    # is therefore what has to rise by exactly that amount.
    try:
        projected = simulate_overtime_impact(employee_fields, SAMPLE_OVERTIME_HOURS)
        expected_credit = SAMPLE_OVERTIME_HOURS / 8
        offset_pots = ("CurrentVacationBalance", "CurrentOvertimeBalance")
        credited = _round(_pot_total(projected, offset_pots) - _pot_total(employee_fields, offset_pots))
        checks.append(_measured(
            "sim_overtime", "simulation",
            label=f"Days credited by {SAMPLE_OVERTIME_HOURS} hours of overtime",
            actual=credited, expected=expected_credit, comparison="equals",
            ok_detail=(
                f"Overtime of {SAMPLE_OVERTIME_HOURS}h credits exactly "
                f"{expected_credit} make-up day(s), vacation offset included."
            ),
            bad_detail=(
                f"Overtime of {SAMPLE_OVERTIME_HOURS}h credited {credited} day(s) "
                f"where {expected_credit} was expected."
            ),
            projected=projected,
        ))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("sim_overtime", "simulation", "fail", f"Overtime simulation raised: {e}"))

    # --- Carryover & Payout dry runs ---
    for code, req_type in [("sim_carry_over", "Carry Over"), ("sim_payout", "Payout")]:
        checks.append(_simulate_co_po_case(code, req_type, employee_fields))

    # --- Requests already in flight ---
    # The only section that looks past the Staff Directory record. A request
    # stalled part-way through processing is hidden from every dashboard, so
    # without these rows a record can measure perfectly while its owner cannot
    # get a request through.
    pending = [r for r in requests if r["status"] == "Pending"]

    missing_amount = [r for r in pending if not r["has_amount"]]
    checks.append(_measured(
        "requests_missing_days", "requests",
        label="Pending requests with no day or hour count",
        actual=len(missing_amount), expected=0, comparison="equals",
        ok_detail="Every pending request has its amount worked out.",
        bad_detail=(
            "Pending request(s) with no amount worked out, so they are hidden "
            "from the dashboards and nobody can action them: "
            + _describe_requests(missing_amount)
        ),
    ))

    missing_manager = [r for r in pending if not r["has_manager"]]
    checks.append(_measured(
        "requests_missing_manager", "requests",
        label="Pending requests with no manager recorded",
        actual=len(missing_manager), expected=0, comparison="equals",
        ok_detail="Every pending request has a manager recorded against it.",
        bad_detail=(
            "Pending request(s) with no manager, so nobody was asked to approve "
            "them and they are hidden from the dashboards: "
            + _describe_requests(missing_manager)
        ),
    ))

    never_asked = [r for r in pending if r["has_manager"] and r["notified"] is False]
    checks.append(_measured(
        "requests_not_notified", "requests",
        label="Pending requests whose manager was never asked",
        actual=len(never_asked), expected=0, comparison="equals",
        ok_detail="Every pending request has been sent to its manager.",
        bad_detail=(
            "Pending request(s) that have a manager but were never sent for "
            "approval: " + _describe_requests(never_asked)
        ),
    ))

    auto_rejected = [r for r in requests if r["auto_rejected"]]
    checks.append(_measured(
        "requests_auto_rejected", "requests",
        label="Requests the system rejected on its own",
        actual=len(auto_rejected), expected=0, comparison="equals",
        ok_detail="Nothing of theirs has been rejected automatically.",
        bad_detail=(
            "Rejected automatically, with the reason recorded on the request: "
            + _describe_requests(auto_rejected, with_reason=True)
        ),
        bad_status="warn",
    ))

    # Reported, not graded: holding approved dates is normal. It is listed
    # because these are the only requests that can block a new one.
    approved = [r for r in requests if r["status"] == "Approved"]
    checks.append(_check(
        "requests_approved_dates", "requests", "pass",
        (
            "Approved requests holding dates: " + _describe_requests(approved)
            if approved else "No approved requests are holding dates."
        ),
        measure={
            "label": "Approved requests holding dates",
            "actual": len(approved),
            "expected": None,
            "comparison": "reported",
        },
    ))

    # Current balances, so the UI can show a before -> after preview per request type.
    current_balances: dict = {}
    for pot in BALANCE_POTS:
        try:
            current_balances[pot] = float(employee_fields.get(pot) or 0)
        except (TypeError, ValueError):
            current_balances[pot] = None

    # Countable summary. Rows that only report a number carry no expectation, so
    # they are left out of the tally rather than counted as automatic passes.
    graded = [
        c for c in checks
        if c["measure"] and c["measure"]["comparison"] != "reported"
    ]

    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "overall": _overall(checks),
        "measurements": {
            "total": len(graded),
            "within_range": sum(1 for c in graded if c["status"] == "pass"),
        },
        "current_balances": current_balances,
        "checks": checks,
    }


def _round(value: float) -> float:
    """Round a balance difference so float noise cannot fail an exact comparison."""
    return round(float(value), 3)


def _as_float(value) -> float:
    """Read a balance value, treating anything unreadable as zero.

    Both wordings passed to `_measured` are built before the verdict is known,
    so a value that cannot be parsed must not raise while composing the message
    that would only have been used had it parsed. A non-numeric pot is reported
    by its own check.

    Args:
        value: Whatever the field held.

    Returns:
        The value as a float, or 0.0 if it is not a number.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pot_total(fields: dict, pots) -> float:
    """Sum the named balance pots from a Staff Directory or projected-balance dict.

    Args:
        fields: Either the employee's fields or a `simulate_*` result.
        pots: Which pot names to add up.

    Returns:
        Their total, treating anything unreadable as zero.
    """
    return sum(_as_float(fields.get(pot)) for pot in pots)


def summarise_request(kind: str, item: dict) -> dict:
    """Reduce one request list row to the facts the setup check grades.

    Args:
        kind: "leave", "overtime" or "carryover-payout".
        item: The raw {"id", "fields"} row as SharePoint returns it.

    Returns:
        A flat summary: its status, whether the amount and the manager were
        recorded, whether the manager was ever asked, the dates it covers, and
        any automatic-rejection reason recorded in its title.
    """
    f = item.get("fields", {})

    # Overtime is entered in hours; the other two lists count days.
    amount_field = "Hours" if kind == "overtime" else "Days"
    try:
        amount = float(f.get(amount_field) or 0)
    except (TypeError, ValueError):
        amount = 0.0

    # Only leave records whether its approval request actually went out. The
    # other lists have no equivalent flag, so None reads as "not known" rather
    # than pretending it was sent.
    notified = f.get("ApproveProcessedFlag") == "Processed" if kind == "leave" else None

    # Every automatic rejection appends its reason to the title, which is the
    # only place the cause survives.
    title = f.get("Title") or ""
    auto_reject_reason = None
    if AUTO_REJECT_MARKER in title:
        auto_reject_reason = title.split(AUTO_REJECT_MARKER, 1)[1].rstrip("]").strip()

    # Carry-over/payout rows only gain a status once actioned, so an unactioned
    # one is pending in everything but name.
    status = f.get("Status") or ("Pending" if kind == "carryover-payout" else "")

    return {
        "kind": kind,
        "item_id": str(item.get("id", "")),
        "status": status,
        "has_amount": amount > 0,
        "has_manager": bool(f.get("ManagerLookupId")),
        "notified": notified,
        "start_date": str(f.get("StartDate") or "")[:10],
        "end_date": str(f.get("EndDate") or "")[:10],
        "auto_rejected": auto_reject_reason is not None,
        "auto_reject_reason": auto_reject_reason,
    }


def _describe_requests(items: list[dict], with_reason: bool = False) -> str:
    """Render request summaries as a readable list for a check's detail line.

    Args:
        items: Summaries produced by `summarise_request`.
        with_reason: Include the recorded automatic-rejection reason.

    Returns:
        A comma-separated description, e.g. "leave #12 (2026-09-01 to 2026-09-05)".
    """
    parts = []
    for r in items:
        dates = r["start_date"]
        if r["end_date"] and r["end_date"] != r["start_date"]:
            dates = f"{r['start_date']} to {r['end_date']}"
        part = f"{r['kind']} #{r['item_id']}" + (f" ({dates})" if dates else "")
        if with_reason and r["auto_reject_reason"]:
            part += f" — {r['auto_reject_reason']}"
        parts.append(part)
    return ", ".join(parts)


def _simulate_leave_case(code, leave_type, fields, days, is_next_year) -> dict:
    """Dry-run one leave type and check the arithmetic, not just that it ran.

    Which pot a day comes out of depends on the balances, because spending
    cascades between them. What must always hold is that the four pots together
    fall by exactly the days taken — the cascade passes a shortfall down the
    chain rather than dropping it. Asserting the total keeps this check honest
    without restating the cascade rules here.

    Args:
        code: Stable check identifier.
        leave_type: SharePoint leave type being simulated.
        fields: The employee's current Staff Directory values.
        days: How many days to simulate.
        is_next_year: Take the next-year cascade branch.

    Returns:
        One report row carrying the days actually deducted.
    """
    label = f"{leave_type}{' (next-year)' if is_next_year else ''}"
    try:
        projected = simulate_leave_impact(fields, leave_type, days, is_next_year)
    except Exception as e:  # noqa: BLE001
        return _check(code, "simulation", "fail", f"{leave_type} simulation raised: {e}")

    if projected is None:
        # Bereavement and jury duty cost nothing — that is the correct outcome,
        # measured rather than assumed.
        return _measured(
            code, "simulation",
            label=f"Days deducted by {label}",
            actual=0.0, expected=0.0, comparison="equals",
            ok_detail=f"{leave_type}: no balance impact, as expected for this type.",
            bad_detail=f"{leave_type}: expected no balance impact.",
        )

    deducted = _round(_pot_total(fields, LEAVE_POTS) - _pot_total(projected, LEAVE_POTS))
    return _measured(
        code, "simulation",
        label=f"Days deducted by {label}",
        actual=deducted, expected=days, comparison="equals",
        ok_detail=f"{label}: {days} day(s) deducts exactly {days} across the balance pots.",
        bad_detail=(
            f"{label}: {days} day(s) deducted {deducted} across the balance pots. "
            "The cascade is losing or inventing days."
        ),
        projected=projected,
    )


def _simulate_co_po_case(code, req_type, fields) -> dict:
    """Dry-run a carry-over or payout and check both sides of the transfer.

    Args:
        code: Stable check identifier.
        req_type: "Carry Over" or "Payout".
        fields: The employee's current Staff Directory values.

    Returns:
        One report row measuring how many sides of the transfer moved the
        expected amount.
    """
    target_pot = "CarryOver" if req_type == "Carry Over" else "Payout"
    try:
        projected = simulate_carryover_payout_impact(fields, SAMPLE_CO_PO_DAYS, req_type)
    except Exception as e:  # noqa: BLE001
        return _check(code, "simulation", "fail", f"{req_type} simulation raised: {e}")

    if projected is None:
        # A would-be-declined carryover/payout is a valid outcome (the employee
        # just has no vacation to move), not a setup problem - keep it a pass and
        # surface it only in the preview.
        return _check(
            code, "simulation", "pass",
            f"{req_type} of {SAMPLE_CO_PO_DAYS} day would be declined: not enough "
            "vacation to move. Expected with the current balance.",
        )

    # A transfer has two sides and both must move by the same amount; measuring
    # only the destination would miss vacation not being debited.
    out_of_vacation = _round(
        _pot_total(fields, ["CurrentVacationBalance"])
        - _pot_total(projected, ["CurrentVacationBalance"])
    )
    into_target = _round(_pot_total(projected, [target_pot]) - _pot_total(fields, [target_pot]))
    sides_correct = sum(1 for moved in (out_of_vacation, into_target) if moved == SAMPLE_CO_PO_DAYS)

    return _measured(
        code, "simulation",
        label=f"Sides of the {req_type} transfer moving exactly {SAMPLE_CO_PO_DAYS} day",
        actual=sides_correct, expected=2, comparison="equals",
        ok_detail=(
            f"{req_type} of {SAMPLE_CO_PO_DAYS} day takes {SAMPLE_CO_PO_DAYS} out of "
            f"vacation and adds it to {target_pot}."
        ),
        bad_detail=(
            f"{req_type} of {SAMPLE_CO_PO_DAYS} day took {out_of_vacation} out of "
            f"vacation and added {into_target} to {target_pot}."
        ),
        projected=projected,
    )


# --- Thin async wrapper: SharePoint reads, then hand off to the pure core ---

async def _check_identity(employee_id, fields: dict) -> dict:
    """Reproduce the email -> M365 user -> Staff Directory round-trip a real
    request relies on, and confirm it lands back on THIS employee."""
    email = (fields.get("EmailAddress") or "").strip()
    if not email:
        return {"status": "fail", "account_count": 0, "detail": (
            "There is no email address on the record, so a submitted request cannot "
            "be linked back to this person."
        )}
    lookup_id = await _resolve_user_lookup_id(email)
    if not lookup_id:
        return {"status": "fail", "account_count": 0, "detail": (
            f"The email {email} was not found in the Microsoft 365 directory, so a "
            "request from this person would not match their record."
        )}
    # From here the email resolved, so the account count is 1; what can still go
    # wrong is which Staff Directory record it lands on.
    resolved = await resolve_person_field(lookup_id)
    if not resolved:
        return {"status": "fail", "account_count": 1, "detail": (
            "Their Microsoft 365 account did not match any Staff Directory record "
            "(their display name likely differs from their name in the directory)."
        )}
    if str(resolved.get("id")) != str(employee_id):
        other = resolved.get("fields", {}).get("Title", "")
        return {"status": "fail", "account_count": 1, "detail": (
            f"Their email/name matches a different person in the directory "
            f"({other}, #{resolved.get('id')}), not this record."
        )}
    return {"status": "pass", "account_count": 1, "detail": (
        "Their email matches their Microsoft 365 account and their Staff Directory "
        f"record ({resolved.get('fields', {}).get('Title', '')})."
    )}


async def _count_manager_m365_matches(managers: list[dict]) -> int:
    """Count supervisors whose email resolves to a Microsoft 365 account.

    This is the value the request path needs before it can record a manager
    against a request, resolved through the same helper that path uses.

    Args:
        managers: Supervisors already resolved to Staff Directory records.

    Returns:
        How many of them have an email that resolves.
    """
    matched = 0
    for manager in managers:
        email = (manager.get("fields", {}).get("EmailAddress") or "").strip()
        if email and await _resolve_user_lookup_id(email):
            matched += 1
    return matched


async def _count_same_name_others(employee_id: str | int, name: str) -> int:
    """Count other Staff Directory records sharing this display name.

    Args:
        employee_id: The record being validated, excluded from the count.
        name: Their display name.

    Returns:
        How many other records carry the same name.
    """
    if not name:
        return 0
    staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    return sum(
        1 for s in staff
        if s.get("fields", {}).get("Title") == name and str(s.get("id")) != str(employee_id)
    )


async def _fetch_employee_requests(employee_id: str | int, submitter_lookup_id) -> list[dict]:
    """Read this employee's own requests from the three request lists.

    Matching mirrors how each list records its submitter: leave and overtime by
    the Microsoft 365 lookup id on their person column, carry-over/payout by the
    Staff Directory id it stores directly. The columns are not indexed, so each
    list is fetched whole and filtered here — the same approach the request
    services already take.

    A list that cannot be read is logged and skipped, so one unavailable list
    degrades that section rather than failing the whole report.

    Args:
        employee_id: Staff Directory item id.
        submitter_lookup_id: Their Microsoft 365 lookup id, or None if unresolved.

    Returns:
        Summaries produced by `summarise_request`.
    """
    summaries: list[dict] = []

    # Without the lookup id there is no way to tell their rows from anyone
    # else's, so these two lists are skipped rather than guessed at.
    if submitter_lookup_id:
        for kind, list_id, person_column in (
            ("leave", settings.SP_LIST_LEAVE_REQUESTS, "SubmittedTest"),
            ("overtime", settings.SP_LIST_OVERTIME_REQUESTS, "SubmittedBy"),
        ):
            try:
                items = await sp_client.get_list_items(list_id)
            except Exception:  # noqa: BLE001 - one unreadable list must not sink the report
                logger.exception("Setup check: could not read the %s request list", kind)
                continue
            for item in items:
                if _extract_lookup_id(item.get("fields", {}), person_column) == submitter_lookup_id:
                    summaries.append(summarise_request(kind, item))

    # Carry-over/payout stores the Staff Directory id outright, so it works even
    # when the Microsoft 365 lookup failed.
    try:
        co_po_items = await sp_client.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
    except Exception:  # noqa: BLE001
        logger.exception("Setup check: could not read the carry-over/payout list")
        co_po_items = []
    for item in co_po_items:
        if str(item.get("fields", {}).get("EmployeeID") or "") == str(employee_id):
            summaries.append(summarise_request("carryover-payout", item))

    return summaries


async def validate_employee_setup(employee_id: str | int) -> dict:
    """Run the full read-only validation suite for one employee. Zero writes.

    Args:
        employee_id: Staff Directory item id to validate.

    Returns:
        The report produced by `build_validation_report`, or a single failing
        check when no such record exists.
    """
    employee = await get_employee_by_id(employee_id)
    if not employee:
        return {
            "employee_id": str(employee_id),
            "employee_name": "",
            "overall": "fail",
            "measurements": {"total": 0, "within_range": 0},
            "current_balances": {},
            "checks": [_check(
                "employee_record", "identity", "fail",
                "No Staff Directory record found for this id.",
            )],
        }

    fields = employee.get("fields", {})
    name = fields.get("Title", "")

    identity = await _check_identity(employee_id, fields)
    managers = await get_all_managers_for_employee(employee)
    all_managers_count = _count_all_managers(fields)

    # Resolved once here and reused: it decides both whether supervisors can be
    # recorded against a request and which request rows belong to this person.
    manager_m365_matches = await _count_manager_m365_matches(managers)
    same_name_others = await _count_same_name_others(employee_id, name)
    submitter_lookup_id = await _resolve_user_lookup_id((fields.get("EmailAddress") or "").strip())
    requests = await _fetch_employee_requests(employee_id, submitter_lookup_id)

    province: str | None = None
    province_error: str | None = None
    holidays: list[dict] = []
    try:
        province = map_location_to_province(fields.get("Location", ""))
    except ValueError as e:
        province_error = str(e)
    if province:
        holidays = await get_holidays_for_province(province)

    sample_start, sample_end = _sample_weekday_range()

    report = build_validation_report(
        employee_id=str(employee_id),
        employee_name=name,
        employee_fields=fields,
        managers=managers,
        all_managers_count=all_managers_count,
        manager_m365_matches=manager_m365_matches,
        same_name_others=same_name_others,
        identity=identity,
        province=province,
        province_error=province_error,
        holidays=holidays,
        requests=requests,
        sample_start=sample_start,
        sample_end=sample_end,
    )
    logger.info(
        "Employee setup validation for #%s (%s): overall=%s, %s of %s measurements in range",
        employee_id, name, report["overall"],
        report["measurements"]["within_range"], report["measurements"]["total"],
    )
    return report
