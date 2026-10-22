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

SAFETY CONTRACT: this module performs READS ONLY - SharePoint lists plus one
SELECT against the approval-state table, which is how it knows whether a
manager was ever asked. It must never call `create_list_item`,
`update_list_item_fields`, `send_email`, `send_email_with_dashboard`,
`send_approval_email`, or `send_sms`. A guard test
(`tests/test_employee_validation.py`) enforces that it stays side-effect free -
keep it that way.

The suite is split like the balance engine: `build_validation_report(...)` is a
pure function over already-fetched inputs (trivially unit-testable), and
`validate_employee_setup(...)` is the thin async wrapper that does the SharePoint
reads and feeds the pure core.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import and_, func, or_, select

from app.config import settings
from app.database import async_session
from app.graph.sharepoint import sp_client
from app.models import RequestApprovalState
from app.repositories import get_holiday_repository
from app.services.employee import (
    get_all_managers_for_employee,
    get_employee_by_id,
    map_location_to_province,
    resolve_person_field,
)
from app.services.app_settings import toronto_today  # Toronto calendar day for the booking policy (#407)
from app.services.employment import is_hourly
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.overlap_detection import _extract_lookup_id
from app.services.holidays import get_holidays_for_province, get_half_friday_season
from app.services.business_days import calculate_business_days
from app.services.balance import (
    LEAVE_BOOKING_TYPES,
    booking_limit,
    simulate_leave_impact,
    simulate_overtime_impact,
    simulate_carryover_payout_impact,
)
from app.repositories.request_store import request_store, requests_in_postgres
from app.repositories.staff_store import staff_in_postgres, staff_store  # Staff Directory reads follow staff_storage
from app.services.staff_cleanup import clean_email, collapse_whitespace  # the normal form names/emails are held to

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
    "Payout": (0, 5),  # approval refuses a payout that would push the total past 5
}
# Carry-over is left out: a negative carry-over is not a setup problem and is
# not fixed from Employee setup (ruled 2026-10-22).

# The four pots leave spending moves between. The cascade passes a deficit down
# the chain rather than clamping it, so their total falls by exactly the days
# taken — which is the arithmetic the simulation checks assert.
LEAVE_POTS = [
    "CurrentVacationBalance",
    "CurrentSickDayBalance",
    "CurrentOvertimeBalance",
    "CarryOver",
]

# The check categories the whole-directory sweep keeps. Simulations and
# requests-in-flight are dropped there: the sweep grades the Staff Directory
# record itself, and a request already in flight belongs to the stuck-request
# view, which reports it per request rather than per employee.
SETUP_LIST_CATEGORIES = ("identity", "supervisor", "location", "balances")

# Written into the Title by every automatic rejection; see auto_reject_titles.
AUTO_REJECT_MARKER = "[Auto-Rejected:"

# How far back an automatic rejection still counts as a live setup problem.
# Without a window, one rejection years ago leaves the check amber forever and
# the signal stops meaning anything.
AUTO_REJECT_RECENT_DAYS = 90


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


def _unmeasured(code: str, category: str, *, label: str, detail: str) -> dict:
    """A check whose input could not be read, deliberately left out of the tally.

    "We could not look" and "we looked and it is wrong" call for different
    actions, so a failed read must never be reported as a failed measurement.
    Rows built here carry the 'reported' comparison, which excludes them from
    the count of measurements in range.

    Args:
        code: Stable check identifier.
        category: Grouping shown in the technical breakdown.
        label: Human name for what would have been measured.
        detail: Sentence explaining what could not be read.

    Returns:
        One warn row carrying an empty measurement.
    """
    return _check(
        code, category, "warn", detail,
        measure={"label": label, "actual": None, "expected": None, "comparison": "reported"},
    )


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
    manager_m365_matches: int | None,
    same_name_others: int | None,
    identity: dict,
    province: str | None,
    province_error: str | None,
    holidays: list[dict],
    requests: list[dict],
    sample_start: date,
    sample_end: date,
    today: date,
    toronto_day: date | None = None,
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
            needs before it can record a manager against a request. None when
            the Microsoft 365 directory could not be read.
        same_name_others: Other staff sharing this display name, or None when
            the Staff Directory could not be re-read to count them.
        identity: Pre-computed round-trip result {"status", "detail", "account_count"}.
        province: Province mapped from their location, or None.
        province_error: Why the mapping failed, when it did.
        holidays: Holiday rows already fetched for that province.
        requests: Their existing requests, summarised by `summarise_request`.
        sample_start: First day of the sample range used for the day calculation.
        sample_end: Last day of that range.
        today: The date the report is being run, used to decide which automatic
            rejections are still recent enough to matter.
        toronto_day: Today in Toronto, used only for the booking-limit check so it
            matches submission/approval/preview (which all read ``toronto_today``);
            defaults to ``toronto_today()`` when not supplied. ``today`` is left
            on the server clock so the auto-rejection window is unchanged (#407).

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
            "label": "Accounts matching their email",
            "actual": identity.get("account_count", 0),
            "expected": 1,
            "comparison": "equals",
        },
    ))

    # A submitted request is matched back to a person through their display
    # name, so a name shared with someone else routes to whichever record is
    # found first.
    if same_name_others is None:
        checks.append(_unmeasured(
            "identity_unique_name", "identity",
            label="Other staff sharing this display name",
            detail=(
                "The staff list could not be re-read, so it is unknown "
                "whether anyone else shares this name."
            ),
        ))
    else:
        checks.append(_measured(
            "identity_unique_name", "identity",
            label="Other staff sharing this display name",
            actual=same_name_others, expected=0, comparison="equals",
            ok_detail="Their name is unique among staff.",
            bad_detail=(
                f"{same_name_others} other staff record(s) are also named "
                f"'{employee_name}'. Requests are matched back to a person by name, "
                "so theirs can be attached to the wrong record."
            ),
        ))

    # A name or email that carries stray spacing (a trailing space, a doubled
    # space, a non-breaking space) is not in the form the request path matches
    # on, so the person is matched to the wrong record or to none. Email case is
    # left alone here: emails are compared without case, so a capital is harmless. This is exactly what left Glenn Clarke managing nobody.
    title_raw = employee_fields.get("Title") or ""
    email_raw = employee_fields.get("EmailAddress") or ""
    spacing_bad = []                                   # which identity fields are not in normal form
    if title_raw != collapse_whitespace(title_raw):    # name differs from its tidied form
        spacing_bad.append("name")
    if email_raw.lower() != clean_email(email_raw):    # email has whitespace (case ignored: harmless)
        spacing_bad.append("email")
    checks.append(_measured(
        "identity_name_spacing", "identity",
        label="Identity fields in their normal form",
        actual=len(spacing_bad), expected=0, comparison="equals",
        ok_detail="The name and email have no stray spaces.",
        bad_detail=(
            "Stray spacing in the "
            + " and ".join(spacing_bad)
            + ". Requests are matched by an exact name and email, so this one can "
            "route to the wrong record or to none. Retype it in their profile and save."
        ),
    ))

    # --- Supervisor / AllManagers ---
    checks.append(_measured(
        "supervisor_set", "supervisor",
        label="Supervisors listed",
        actual=all_managers_count, expected=1, comparison="at_least",
        ok_detail=f"{all_managers_count} supervisor(s) listed.",
        bad_detail=(
            "No supervisor is set on their staff record, so a request "
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
        if manager_m365_matches is None:
            # The directory read failed. Reporting zero matches here would
            # accuse a correctly configured record of being broken.
            checks.append(_unmeasured(
                "manager_m365_match", "supervisor",
                label="Supervisor emails the app recognises",
                detail=(
                    "The account list could not be read, so it is "
                    "unknown whether the supervisor emails resolve. Run the "
                    "check again."
                ),
            ))
        else:
            checks.append(_measured(
                "manager_m365_match", "supervisor",
                label="Supervisor emails the app recognises",
                actual=manager_m365_matches, expected=len(managers), comparison="equals",
                ok_detail="Every supervisor's email is recognised.",
                bad_detail=(
                    f"Only {manager_m365_matches} of {len(managers)} supervisor "
                    "email(s) are recognised. Their requests cannot "
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
        # A warn, not a fail: management groups list the whole group as each
        # member's supervisors, themselves included, and requests still route
        # to the rest of the group. What it permits is self-approval, which is
        # worth knowing about rather than a stalled request.
        checks.append(_measured(
            "supervisor_not_self", "supervisor",
            label="Supervisor entries naming the employee themselves",
            actual=self_supervising, expected=0, comparison="equals",
            ok_detail="No supervisor entry points back at this employee.",
            bad_detail=(
                "This employee is listed among their own supervisors. Their "
                "requests still reach the others listed, but they could approve "
                "their own."
            ),
            bad_status="warn",
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

    # --- Balance pots that do not parse are skipped by the range check below ---
    # (no longer graded on their own: typed staff tables cannot hold text).
    bad_pots = []
    for pot in BALANCE_POTS:
        raw = employee_fields.get(pot)
        try:
            float(raw or 0)
        except (TypeError, ValueError):
            bad_pots.append(f"{pot}={raw!r}")

    # --- Balance pots sit in a sane range ---
    # Only meaningful once they parse; a pot that does not parse is skipped.
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
    # hourly (#300) have no vacation, so a zero vacation grant is right for
    # them and only their sick grant is checked.
    entitlement_fields = (("sick", "SickDayEntitlement"),) if is_hourly(employee_fields) else (
        ("yearly vacation", "DefaultYearlyVacationDays"),
        ("sick", "SickDayEntitlement"),
    )
    missing_entitlements = [
        label for label, field in entitlement_fields
        if not _as_float(employee_fields.get(field)) > 0
    ]
    checks.append(_measured(
        "entitlements_set", "balances",
        label="Yearly entitlements with a value above zero",
        actual=len(entitlement_fields) - len(missing_entitlements),
        expected=len(entitlement_fields), comparison="equals",
        ok_detail=(
            "The sick entitlement is set (hourly staff have no vacation entitlement)."
            if len(entitlement_fields) == 1 else
            "Both the yearly vacation and sick entitlements are set."
        ),
        bad_detail=(
            "No entitlement recorded for: " + ", ".join(missing_entitlements)
            + ". Their yearly grant cannot be worked out from this record."
        ),
        bad_status="warn",
    ))

    # --- Employment type fits the record ---
    # Hourly staff get no vacation grant and bank no vacation (#300), so a
    # vacation grant or a positive vacation bank on an hourly record means the
    # employment type, or those values, are wrong. A negative vacation balance
    # is left alone: hourly BC staff carry one from before the hourly rules.
    if is_hourly(employee_fields):
        vacation_grant = _as_float(employee_fields.get("DefaultYearlyVacationDays"))   # hourly: should be 0
        vacation_bank = _as_float(employee_fields.get("CurrentVacationBalance"))       # hourly: never above 0
        salaried_values = []                                      # each salaried value found, in words
        if vacation_grant > 0:
            salaried_values.append(f"a yearly vacation grant of {vacation_grant:g} days")
        if vacation_bank > 0:
            salaried_values.append(f"a vacation balance of {vacation_bank:g} days")
        checks.append(_measured(
            "employment_type_fits", "balances",
            label="Salaried vacation values on an hourly record",
            actual=len(salaried_values), expected=0, comparison="equals",
            ok_detail="The record is hourly and carries no salaried vacation values.",
            bad_detail=(
                f"The record is {(employee_fields.get('SalaryHourly') or '').strip()} but has "
                + " and ".join(salaried_values)
                + ", which only salaried staff carry."
            ),
        ))

    # --- Per-leave-type dry runs (carry-over available) ---
    for code, leave_type, days in LEAVE_TYPE_CASES:
        checks.append(_simulate_leave_case(code, leave_type, employee_fields, days, carryover_available=True))

    # --- Carry-over-expired branch (a Vacation request dated past the expiry, #407) ---
    checks.append(_simulate_leave_case(
        "sim_next_year_vacation", "Vacation", employee_fields, SAMPLE_LEAVE_DAYS, carryover_available=False,
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

    # Whether a manager was actually asked is answered by the approval-state
    # table, which gains a row the moment an approval email is composed. Nothing
    # on the SharePoint item records it: ApproveProcessedFlag means "a decision
    # has been applied", so reading it here would mark every healthy pending
    # request as never sent.
    with_manager = [r for r in pending if r["has_manager"]]
    never_asked = [r for r in with_manager if r["notified"] is False]
    unknown_notified = [r for r in with_manager if r["notified"] is None]
    if not never_asked and unknown_notified:
        # Nothing proven unsent, but the send record was unreadable — say so
        # instead of reporting a clean result nobody verified.
        checks.append(_unmeasured(
            "requests_not_notified", "requests",
            label="Pending requests whose manager was never asked",
            detail=(
                "The record of which approval emails went out could not be read, "
                "so it is unknown whether these reached a manager: "
                + _describe_requests(unknown_notified)
            ),
        ))
    else:
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

    # Only recent rejections count. An old one is history the employee has long
    # since worked around; grading it would leave this check amber for good and
    # the colour would stop carrying information. A row with no creation date is
    # kept rather than dropped, so nothing disappears on a missing value.
    cutoff = (today - timedelta(days=AUTO_REJECT_RECENT_DAYS)).isoformat()
    auto_rejected = [
        r for r in requests
        if r["auto_rejected"] and (not r["created_date"] or r["created_date"] >= cutoff)
    ]
    checks.append(_measured(
        "requests_auto_rejected", "requests",
        label=f"Requests the system rejected on its own in the last {AUTO_REJECT_RECENT_DAYS} days",
        actual=len(auto_rejected), expected=0, comparison="equals",
        ok_detail=(
            f"Nothing of theirs has been rejected automatically in the last "
            f"{AUTO_REJECT_RECENT_DAYS} days."
        ),
        bad_detail=(
            "Rejected automatically, with the reason recorded on the request: "
            + _describe_requests(auto_rejected, with_reason=True)
        ),
        bad_status="warn",
    ))

    # Pending vacation/part-day requests already past the booking limit (#407).
    # These will be refused at approval, so flag them before a manager tries.
    # Hourly staff are never limited, so the check is skipped for them.
    vacation_now = _as_float(employee_fields.get("CurrentVacationBalance"))
    carry_now = _as_float(employee_fields.get("CarryOver"))
    # The booking limit reads the Toronto day, like submission/approval/preview, so
    # the setup screen flags exactly what approval would refuse (UTC on Railway can
    # be a day ahead) (#407). Falls back to today's Toronto date when not passed.
    booking_today = toronto_day if toronto_day is not None else toronto_today()
    limit = booking_limit(vacation_now, carry_now, booking_today)  # the person's current booking limit
    past_limit = []
    if not is_hourly(employee_fields):
        for r in pending:
            if r["kind"] != "leave" or r.get("leave_type", "") not in LEAVE_BOOKING_TYPES:
                continue                                           # only vacation and part days are booked
            last_day = r.get("end_date") or r.get("start_date") or ""   # "YYYY-MM-DD", or "" when undated
            if last_day and last_day > limit.isoformat():          # ISO strings compare chronologically
                past_limit.append(r)
    checks.append(_measured(
        "pending_over_booking_limit", "requests",
        label="Pending vacation requests past the booking limit",
        actual=len(past_limit), expected=0, comparison="equals",
        ok_detail="No pending vacation request runs past what they can book.",
        bad_detail=(
            f"Pending vacation request(s) dated past {limit:%b} {limit.day}, {limit.year}, "
            "which will be refused at approval: " + _describe_requests(past_limit)
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


def summarise_request(kind: str, item: dict, notified: bool | None = None) -> dict:
    """Reduce one request list row to the facts the setup check grades.

    Args:
        kind: "leave", "overtime" or "carryover-payout".
        item: The raw {"id", "fields"} row as SharePoint returns it.
        notified: Whether an approval email was actually composed for this
            request, read from the approval-state table by the caller. None
            means that record could not be read — which is not the same as a
            manager never having been asked. Nothing on the SharePoint item
            answers this question, so it is never inferred here.

    Returns:
        A flat summary: its status, whether the amount and the manager were
        recorded, whether the manager was asked, the dates it covers, when it
        was raised, and any automatic-rejection reason recorded in its title.
    """
    f = item.get("fields", {})

    # Overtime is entered in hours; the other two lists count days.
    amount_field = "Hours" if kind == "overtime" else "Days"
    try:
        amount = float(f.get(amount_field) or 0)
    except (TypeError, ValueError):
        amount = 0.0

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
        "leave_type": f.get("LeaveType") or "",                 # for the booking-limit check (#407); "" for non-leave
        "has_amount": amount > 0,
        "has_manager": bool(f.get("ManagerLookupId") or f.get("ManagerID")),   # SharePoint lookup id, or the staff id on Postgres
        "notified": notified,
        "start_date": str(f.get("StartDate") or "")[:10],
        "end_date": str(f.get("EndDate") or "")[:10],
        # Graph stamps this on every row; sliced to a plain YYYY-MM-DD so it can
        # be compared against a cutoff as a string, with nothing to fail parsing.
        "created_date": str(item.get("createdDateTime") or "")[:10],
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


def _simulate_leave_case(code, leave_type, fields, days, carryover_available=True) -> dict:
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
        carryover_available: False dry-runs a request past the carry-over expiry,
            which skips carry-over in the cascade (#407).

    Returns:
        One report row carrying the days actually deducted.
    """
    label = f"{leave_type}{'' if carryover_available else ' (carry-over expired)'}"
    try:
        projected = simulate_leave_impact(fields, leave_type, days, carryover_available)
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

def _identity_verdict(employee_id, email: str, lookup_id, resolved: dict | None) -> dict:
    """Turn one already-performed round-trip into the identity verdict.

    Pure, and the single place the identity wording lives, so the one-employee
    check and the whole-directory sweep can resolve the round-trip by different
    means (production resolvers vs. maps built from one read) without their
    verdicts drifting apart.

    Args:
        employee_id: Staff Directory item id being validated.
        email: Their email, already stripped. Empty when the record has none.
        lookup_id: The Microsoft 365 lookup id their email resolved to, or None.
        resolved: The Staff Directory record that lookup id led back to, or None.

    Returns:
        {"status", "account_count", "detail", "lookup_id"} - lookup_id is None
        when the email never resolved.
    """
    if not email:
        return {"status": "fail", "account_count": 0, "lookup_id": None, "detail": (
            "There is no email address on the record, so a submitted request cannot "
            "be linked back to this person."
        )}
    if not lookup_id or not resolved:
        # No sign-in account behind the email, or one whose name finds no record:
        # neither matters any more. The request page links a request by email, so
        # the email on the record is the whole link (ruled 2026-10-22).
        return {"status": "pass", "account_count": 1 if lookup_id else 0, "lookup_id": lookup_id, "detail": (
            f"The request page links requests from {email} to this record."
        )}
    if str(resolved.get("id")) != str(employee_id):
        other = resolved.get("fields", {}).get("Title", "")
        other_email = (resolved.get("fields", {}).get("EmailAddress") or "").strip().lower()
        if other_email == email.lower():
            # A shared mailbox (e.g. Accounts Payable and the person who reads it):
            # both records are real, and the request page asks which one signs in.
            return {"status": "pass", "account_count": 1, "lookup_id": lookup_id, "detail": (
                f"This email is shared with {other} (#{resolved.get('id')}). The request "
                "page asks which of them is signing in, so each gets their own requests."
            )}
        return {"status": "fail", "account_count": 1, "lookup_id": lookup_id, "detail": (
            f"Their email/name matches a different person in the directory "
            f"({other}, #{resolved.get('id')}), not this record."
        )}
    return {"status": "pass", "account_count": 1, "lookup_id": lookup_id, "detail": (
        "Their email matches their staff "
        f"record ({resolved.get('fields', {}).get('Title', '')})."
    )}


async def _check_identity(employee_id, fields: dict) -> dict:
    """Reproduce the email -> M365 user -> Staff Directory round-trip a real
    request relies on, and confirm it lands back on THIS employee.

    Deliberately calls the production resolver rather than a local copy: the
    point of this check is that the employee's own identity is resolved by the
    exact code a submitted request runs. The lookup id it resolves is returned
    so the rest of the report can reuse it instead of resolving it a second
    time.

    Args:
        employee_id: Staff Directory item id being validated.
        fields: Their Staff Directory field values.

    Returns:
        {"status", "account_count", "detail", "lookup_id"} - lookup_id is None
        when the email never resolved.
    """
    if await staff_in_postgres():
        # After the switch a request links to its submitter by email against the
        # staff table, so that table is the directory; SharePoint is never read.
        staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
        directory, id_to_name = _staff_as_directory(staff)
        return _identity_from_maps(str(employee_id), fields, directory, id_to_name, _index_staff_by_name(staff))
    email = (fields.get("EmailAddress") or "").strip()
    if not email:
        return _identity_verdict(employee_id, email, None, None)
    lookup_id = await _resolve_user_lookup_id(email)
    if not lookup_id:
        return _identity_verdict(employee_id, email, None, None)
    return _identity_verdict(employee_id, email, lookup_id, await resolve_person_field(lookup_id))


def _staff_as_directory(staff: list[dict]) -> tuple[dict[str, int], dict[int, str]]:
    """The staff table read as the directory, for after the switch.

    Gives the same two maps ``_parse_m365_rows`` builds from the Microsoft 365
    user list, keyed by staff id instead of a SharePoint lookup id, so every
    identity and supervisor check runs unchanged on Postgres.

    Args:
        staff: Staff rows in the ``{"id","fields"}`` shape.

    Returns:
        (lowercased email -> staff id, staff id -> name). The first row with an
        email keeps it, as the staff import does with a duplicate.
    """
    by_email: dict[str, int] = {}
    by_id: dict[int, str] = {}
    for row in staff:
        try:
            staff_id = int(row["id"])                       # staff ids are whole numbers
        except (KeyError, TypeError, ValueError):
            continue                                        # unmatchable either way
        fields = row.get("fields", {})
        email = (fields.get("EmailAddress") or "").strip().lower()
        if email:
            by_email.setdefault(email, staff_id)            # first wins on a duplicate
        name = fields.get("Title", "")
        if name:
            by_id[staff_id] = name
    return by_email, by_id


async def _fetch_m365_rows() -> list[dict] | None:
    """Read the Microsoft 365 User Information List once.

    Returns:
        The raw rows, or None when the list could not be read at all - which is
        a different answer from "nobody matched".
    """
    try:
        return await sp_client.get_list_items("User Information List", top=5000)
    except Exception:  # noqa: BLE001 - an unreadable directory is reported, not raised
        logger.exception("Setup check: could not read the Microsoft 365 directory")
        return None


def _parse_m365_rows(users: list[dict]) -> tuple[dict[str, int], dict[int, str]]:
    """Split User Information List rows into the two maps the checks need.

    Pure, so one read can serve both the email round-trip (email -> lookup id,
    the rule `_resolve_user_lookup_id` matches on) and the reverse resolution a
    Person/Group field needs (lookup id -> display name, the rule
    `employee._get_sp_user_name_map` uses).

    Args:
        users: Rows as SharePoint returns them.

    Returns:
        (lowercased email -> lookup id, lookup id -> display name). A row whose
        id cannot be read is left out of both, since it can be matched neither
        way.
    """
    by_email: dict[str, int] = {}
    by_lookup_id: dict[int, str] = {}
    for user in users:
        try:
            lookup_id = int(user["id"])
        except (KeyError, TypeError, ValueError):
            continue  # a row without a usable id cannot be matched against
        fields = user.get("fields", {})
        email = (fields.get("EMail") or "").strip().lower()
        if email:
            by_email[email] = lookup_id
        name = fields.get("Title", "")
        if name:
            by_lookup_id[lookup_id] = name
    return by_email, by_lookup_id


async def _load_m365_directory() -> dict[str, int] | None:
    """Read the Microsoft 365 User Information List once, as email -> lookup id.

    The production resolver refetches this list on every call, which is fine for
    one request but not for a report that would otherwise resolve the employee
    and each of their supervisors separately. Reading it once holds the cost at
    a single fetch no matter how many supervisors are listed. The matching rule
    is the same one the resolver uses: case-insensitive on the EMail column,
    filtered in memory because the list is not indexed.

    Returns:
        Lowercased email -> lookup id, or None when the list could not be read
        at all — which is a different answer from "nobody matched".
    """
    if await staff_in_postgres():                           # after the switch: the staff table
        staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
        return _staff_as_directory(staff)[0]
    users = await _fetch_m365_rows()
    if users is None:
        return None
    by_email, _by_lookup_id = _parse_m365_rows(users)
    return by_email


def _count_manager_m365_matches(managers: list[dict], directory: dict[str, int] | None) -> int | None:
    """Count supervisors whose email appears in the Microsoft 365 directory.

    This is the value the request path needs before it can record a manager
    against a request.

    Args:
        managers: Supervisors already resolved to Staff Directory records.
        directory: The map from `_load_m365_directory`, or None if unreadable.

    Returns:
        How many of them resolve, or None when the directory was unreadable.
    """
    if directory is None:
        return None
    return sum(
        1 for manager in managers
        if (manager.get("fields", {}).get("EmailAddress") or "").strip().lower() in directory
    )


async def _count_same_name_others(employee_id: str | int, name: str) -> int | None:
    """Count other Staff Directory records sharing this display name.

    Args:
        employee_id: The record being validated, excluded from the count.
        name: Their display name.

    Returns:
        How many other records carry the same name, or None when the directory
        could not be read — reported as unknown rather than as zero.
    """
    if not name:
        return 0
    try:
        staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    except Exception:  # noqa: BLE001 - one unreadable list must not sink the report
        logger.exception("Setup check: could not re-read the Staff Directory")
        return None
    return sum(
        1 for s in staff
        if s.get("fields", {}).get("Title") == name and str(s.get("id")) != str(employee_id)
    )


async def fetch_approval_email_records(matched: list[tuple]) -> set[tuple[str, str]] | None:
    """Look up which of these requests have had an approval email composed.

    `bump_and_snapshot` inserts a request_approval_state row the moment an
    approval email is built, for all three request types, so the presence of a
    row is the record that a manager was actually asked. Nothing on the
    SharePoint item answers this: ApproveProcessedFlag is set only when a
    decision is applied, so reading it would mark every healthy pending request
    as never sent.

    Args:
        matched: (kind, list_id, item) triples for this employee's requests.

    Returns:
        The (list_id, item_id) pairs that have a row, or None when the table
        could not be read — which must not be reported as "never sent".
    """
    if not matched:
        return set()

    list_ids = {list_id for _, list_id, _ in matched}
    item_ids = {str(item.get("id", "")) for _, _, item in matched}
    try:
        async with async_session() as session:
            rows = await session.execute(
                select(RequestApprovalState.list_id, RequestApprovalState.item_id).where(
                    RequestApprovalState.list_id.in_(list_ids),
                    RequestApprovalState.item_id.in_(item_ids),
                )
            )
            # Filtering on both columns separately can admit a pair that belongs
            # to neither request; membership is tested on the pair, so it cannot
            # produce a false match.
            return {(row.list_id, row.item_id) for row in rows}
    except Exception:  # noqa: BLE001 - an unreadable table is reported as unknown
        logger.exception("Setup check: could not read the approval email records")
        return None


# The url-form kind each caller passes -> the kind stored in requests.kind, so a
# step key is built with the value the outbox rows actually carry (#416).
_URL_TO_STORED_KIND = {"leave": "leave", "overtime": "overtime", "carryover-payout": "carryover_payout"}


async def fetch_manager_asked_records(matched: list[tuple]) -> set[tuple[str, str]] | None:
    """Which of these requests had a manager actually asked, on the Postgres path (#416).

    In Postgres mode the record that a manager was asked is a ``manager_approval``
    outbox row for the request's CURRENT approval version (aligned with
    ``_manager_ever_asked`` in request_submission). An imported request
    (``sp_item_id`` set) was asked by the old SharePoint path, which left no outbox
    row, so it is also accepted when it has a ``request_approval_state`` row (the
    old record of a send). One bulk query per table, never one per request.

    Args:
        matched: (kind, list_id, item) triples, kind in url form.

    Returns:
        The (list_id, item_id) pairs whose manager was asked, or None when the
        tables could not be read (reported as unknown, never as "never asked").
    """
    if not matched:
        return set()
    from app.models.outbox import Outbox
    from app.models.request_record import RequestRecord
    from app.services.request_journal import step_key

    # (stored_kind, item_id_int, list_id, item_id_str) for each readable entry.
    wanted: list[tuple] = []
    for kind, list_id, item in matched:
        try:
            item_id_int = int(item.get("id"))
        except (TypeError, ValueError):
            continue                                              # not a numeric id: cannot match a typed row
        wanted.append((_URL_TO_STORED_KIND.get(kind, kind), item_id_int, list_id, str(item.get("id"))))
    if not wanted:
        return set()
    try:
        async with async_session() as session:
            # Load the typed rows for every wanted (kind, item_id), grouped by kind.
            ids_by_kind: dict[str, set] = {}
            for stored_kind, item_id_int, _, _ in wanted:
                ids_by_kind.setdefault(stored_kind, set()).add(item_id_int)
            records = []
            for stored_kind, ids in ids_by_kind.items():
                records.extend((await session.execute(
                    select(RequestRecord).where(
                        RequestRecord.kind == stored_kind, RequestRecord.item_id.in_(ids)
                    )
                )).scalars().all())
            rec_by_key = {(r.kind, r.item_id): r for r in records}
            rec_ids = [r.id for r in records]

            # The manager_approval outbox rows for those requests, keys collected
            # per request so each is tested against its own current-version prefix.
            keys_by_request: dict[int, list[str]] = {}
            if rec_ids:
                for rid, key in (await session.execute(
                    select(Outbox.request_id, Outbox.idempotency_key).where(
                        Outbox.request_id.in_(rec_ids),
                        Outbox.message_type == "manager_approval",
                        # Count a row as an ask only while it still stands a chance
                        # of reaching the manager: sent, queued, in flight, or a
                        # failed row still scheduled to retry. A permanently failed
                        # or dropped row is NOT an ask, so a request whose every
                        # manager ask failed reads as stuck, not emailed (finding 9).
                        or_(
                            Outbox.status.in_(("sent", "pending", "sending")),
                            and_(Outbox.status == "failed", Outbox.next_attempt_at.isnot(None)),
                        ),
                    )
                )).all():
                    keys_by_request.setdefault(rid, []).append(key)

            # The old approval-state rows, which still count for imported requests.
            list_ids = {list_id for _, _, list_id, _ in wanted}
            item_id_strs = {s for _, _, _, s in wanted}
            state_pairs = {
                (row.list_id, row.item_id) for row in (await session.execute(
                    select(RequestApprovalState.list_id, RequestApprovalState.item_id).where(
                        RequestApprovalState.list_id.in_(list_ids),
                        RequestApprovalState.item_id.in_(item_id_strs),
                    )
                )).all()
            }
    except Exception:  # noqa: BLE001 - an unreadable table is reported as unknown, never as "never asked"
        logger.exception("Setup check: could not read the manager-asked records")
        return None

    asked: set[tuple[str, str]] = set()
    for stored_kind, item_id_int, list_id, item_id_str in wanted:
        record = rec_by_key.get((stored_kind, item_id_int))
        asked_via_outbox = False
        imported = False
        if record is not None:
            imported = record.sp_item_id is not None              # only an imported row trusts the old table
            prefix = step_key(stored_kind, item_id_int, "sent_to_manager", record.approval_version)
            asked_via_outbox = any(
                key.startswith(f"{prefix}:") for key in keys_by_request.get(record.id, [])
            )
        # The old approval-state table proves a send only for an IMPORTED request.
        # An app-made request also gains a state row (from an edit via
        # _sync_version_state / apply_admin_edit) without a manager ever being
        # asked, so it must rely on its own manager_approval outbox row, never that
        # state row (finding 8).
        if asked_via_outbox or (imported and (list_id, item_id_str) in state_pairs):
            asked.add((list_id, item_id_str))
    return asked


async def _fetch_employee_requests(employee_id: str | int, submitter_lookup_id) -> list[dict]:
    """Read this employee's own requests from the three request lists.

    Matching mirrors how each list records its submitter: leave and overtime by
    the Microsoft 365 lookup id on their person column, carry-over/payout by the
    Staff Directory id it stores directly. The columns are not indexed, so each
    list is fetched whole and filtered here — the same approach the request
    services already take.

    Rows are collected first and their approval-email state read in one query
    afterwards, rather than per request.

    A list that cannot be read is logged and skipped, so one unavailable list
    degrades that section rather than failing the whole report.

    Args:
        employee_id: Staff Directory item id.
        submitter_lookup_id: Their Microsoft 365 lookup id, or None if unresolved.

    Returns:
        Summaries produced by `summarise_request`.
    """
    matched: list[tuple[str, str, dict]] = []  # (kind, list_id, raw row)

    # After the switch every request row carries the submitter's staff id, so
    # all three lists match on it (no SharePoint person column involved).
    on_postgres = await staff_in_postgres()
    if on_postgres:
        for kind, list_id in (
            ("leave", settings.SP_LIST_LEAVE_REQUESTS),
            ("overtime", settings.SP_LIST_OVERTIME_REQUESTS),
            ("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT),
        ):
            for item in await request_store.get_list_items(list_id):   # Postgres; an error surfaces
                if str(item.get("fields", {}).get("EmployeeID") or "") == str(employee_id):
                    matched.append((kind, list_id, item))
        submitter_lookup_id = None                          # skip the SharePoint matching below

    # Without the lookup id there is no way to tell their rows from anyone
    # else's, so these two lists are skipped rather than guessed at.
    if submitter_lookup_id:
        for kind, list_id, person_column in (
            ("leave", settings.SP_LIST_LEAVE_REQUESTS, "SubmittedTest"),
            ("overtime", settings.SP_LIST_OVERTIME_REQUESTS, "SubmittedBy"),
        ):
            try:
                items = await request_store.get_list_items(list_id)
            except Exception:  # noqa: BLE001 - one unreadable list must not sink the report
                logger.exception("Setup check: could not read the %s request list", kind)
                continue
            for item in items:
                if _extract_lookup_id(item.get("fields", {}), person_column) == submitter_lookup_id:
                    matched.append((kind, list_id, item))

    # Carry-over/payout stores the Staff Directory id outright, so it works even
    # when the Microsoft 365 lookup failed.
    try:
        # Already matched above on Postgres.
        co_po_items = [] if on_postgres else await request_store.get_list_items(settings.SP_LIST_CARRYOVER_PAYOUT)
    except Exception:  # noqa: BLE001
        logger.exception("Setup check: could not read the carry-over/payout list")
        co_po_items = []
    for item in co_po_items:
        if str(item.get("fields", {}).get("EmployeeID") or "") == str(employee_id):
            matched.append(("carryover-payout", settings.SP_LIST_CARRYOVER_PAYOUT, item))

    # In Postgres mode the record of a manager ask is a manager_approval outbox
    # row (imported rows also honour the old table); in SharePoint mode it is the
    # approval-state row the old send wrote (#416).
    emailed = (
        await fetch_manager_asked_records(matched)
        if await requests_in_postgres()
        else await fetch_approval_email_records(matched)
    )
    return [
        summarise_request(
            kind, item,
            # None propagates as "not known", never as "never sent".
            notified=None if emailed is None else (list_id, str(item.get("id", ""))) in emailed,
        )
        for kind, list_id, item in matched
    ]


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
                "No staff record found for this id.",
            )],
        }

    fields = employee.get("fields", {})
    name = fields.get("Title", "")

    identity = await _check_identity(employee_id, fields)
    managers = await get_all_managers_for_employee(employee)
    all_managers_count = _count_all_managers(fields)

    # The identity check already resolved this employee through the production
    # resolver, so reuse its answer rather than paying for the same list read
    # again. It decides which request rows belong to this person.
    submitter_lookup_id = identity.get("lookup_id")

    # One directory read covers every supervisor, however many are listed.
    directory = await _load_m365_directory()
    manager_m365_matches = _count_manager_m365_matches(managers, directory)
    same_name_others = await _count_same_name_others(employee_id, name)
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
        today=date.today(),
    )
    # The same unmoved-balance flag the list shows, so the single check can offer
    # Settle requests too (#400). It reads Postgres; before the switch it is empty.
    unmoved = (await approved_requests_without_balance_change(str(employee_id))).get(str(employee_id), [])
    report["unmoved_requests"] = unmoved
    if unmoved:
        report["checks"].append(_unmoved_check(unmoved))
        report["overall"] = "fail"                                 # a flagged request is a real problem
    logger.info(
        "Employee setup validation for #%s (%s): overall=%s, %s of %s measurements in range",
        employee_id, name, report["overall"],
        report["measurements"]["within_range"], report["measurements"]["total"],
    )
    return report


# --- Whole-directory sweep: every Staff Directory record, three reads total ---
#
# The per-employee check above answers "is this person set up?" on demand, one
# person at a time, at the cost of a fistful of SharePoint reads each. Nobody
# runs it speculatively, so a record only gets looked at once someone has
# already been blocked by it - which is how an employee with an empty
# AllManagers column had three leave requests stall before anyone noticed.
#
# The sweep answers the same question for everyone at once. It reads the three
# lists ONCE between them, builds the lookups the per-employee path resolves
# with live calls, and feeds the same pure core, so the two cannot disagree
# about what counts as broken.

def _index_staff_by_name(staff: list[dict]) -> dict[str, dict]:
    """Index Staff Directory rows by display name, first match winning.

    `employee.get_employee_by_name` returns the FIRST row whose trimmed,
    lowercased Title matches, so a name shared by two records always resolves to
    the same one. Keeping the first entry here reproduces that, which is what
    lets the sweep spot the second record as pointing at somebody else.

    Args:
        staff: Staff Directory rows.

    Returns:
        Trimmed lowercased name -> the first row carrying it.
    """
    by_name: dict[str, dict] = {}
    for item in staff:
        key = (item.get("fields", {}).get("Title") or "").strip().lower()
        if key and key not in by_name:
            by_name[key] = item
    return by_name


def _count_staff_names(staff: list[dict]) -> dict[str, int]:
    """Count Staff Directory rows per exact display name.

    Exact, not trimmed or lowercased, because `_count_same_name_others` compares
    Title verbatim and the sweep must count the same way.

    Args:
        staff: Staff Directory rows.

    Returns:
        Title -> how many rows carry it.
    """
    counts: dict[str, int] = {}
    for item in staff:
        name = item.get("fields", {}).get("Title", "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _group_holidays_by_province(items: list[dict]) -> dict[str, list[dict]]:
    """Bucket the holiday list by province, in the shape the checks expect.

    `holidays.get_holidays_for_province` filters the whole list per province and
    hands back the field dicts; grouping once produces the same lists without a
    read per employee.

    Args:
        items: Holiday rows.

    Returns:
        Province -> that province's holiday field dicts, in list order.
    """
    grouped: dict[str, list[dict]] = {}
    for item in items:
        fields = item.get("fields", {})
        grouped.setdefault(fields.get("Province", ""), []).append(fields)
    return grouped


def _identity_unverified() -> dict:
    """The identity verdict used when the Microsoft 365 directory is unreadable.

    A failed read must never flag every employee at once: without the directory
    the round-trip was never performed, so there is nothing to fail. It is a
    warn carrying no account count, which is also why `manager_m365_matches` is
    passed as None alongside it.

    Returns:
        {"status", "account_count", "detail", "lookup_id"}.
    """
    return {"status": "warn", "account_count": 0, "lookup_id": None, "detail": (
        "The account list could not be read, so identity could not be "
        "verified. Run the check again."
    )}


def _identity_from_maps(
    employee_id: str,
    fields: dict,
    directory: dict[str, int],
    sp_user_to_name: dict[int, str],
    staff_by_name: dict[str, dict],
) -> dict:
    """Run the identity round-trip against the pre-built maps.

    Same three hops the live check makes - email to Microsoft 365 account, that
    account to a display name, that name back to a Staff Directory record - and
    the same verdicts, resolved from maps rather than a call each.

    Args:
        employee_id: Staff Directory item id being validated.
        fields: Their Staff Directory field values.
        directory: Lowercased email -> lookup id.
        sp_user_to_name: Lookup id -> display name.
        staff_by_name: Trimmed lowercased name -> Staff Directory row.

    Returns:
        The verdict `_identity_verdict` builds.
    """
    email = (fields.get("EmailAddress") or "").strip()
    lookup_id = directory.get(email.lower()) if email else None
    resolved = None
    if lookup_id:
        display_name = sp_user_to_name.get(lookup_id, "")
        if display_name:
            resolved = staff_by_name.get(display_name.strip().lower())
    return _identity_verdict(employee_id, email, lookup_id, resolved)


def _resolve_managers_from_map(fields: dict, staff_by_name: dict[str, dict]) -> list[dict]:
    """Resolve the AllManagers entries against the pre-built name index.

    Mirrors `employee.get_all_managers_for_employee`: entries with no
    LookupValue are skipped, and each name resolves to the first Staff Directory
    row carrying it.

    Args:
        fields: The employee's Staff Directory field values.
        staff_by_name: Trimmed lowercased name -> Staff Directory row.

    Returns:
        The supervisor rows that resolved, in the order they are listed.
    """
    managers: list[dict] = []
    entries = fields.get("AllManagers")
    if not isinstance(entries, list):
        return managers
    for entry in entries:
        name = entry.get("LookupValue", "") if isinstance(entry, dict) else ""
        if not name:
            continue
        manager = staff_by_name.get(name.strip().lower())
        if manager:
            managers.append(manager)
    return managers


def _setup_problem(check: dict) -> dict:
    """Reduce a report row to what the list view shows for it."""
    return {"code": check["code"], "category": check["category"], "detail": check["detail"]}


def _creator_email(record: dict) -> str | None:
    """The email of whoever created this Staff Directory record, or None.

    Graph carries `createdBy` on the list item itself, not inside `fields`. A
    record the app created carries an `application` identity with no `user` at
    all, which is why the absence of an email is answered with None rather than
    with a substitute: nobody to nudge is a real answer, and the only other
    address on the item is the last editor, which is a shared mailbox.

    Args:
        record: The raw list item as SharePoint returns it.

    Returns:
        The creator's email, lowercased, or None when the item was not created
        by a person or carries no address for them.
    """
    user = (record.get("createdBy") or {}).get("user") or {}
    return (user.get("email") or "").strip().lower() or None


# Leave types that never move a balance: the two the app spends nothing on, plus
# the retired "Work from Home", still approved through the old form.
UNMOVED_SKIP_LEAVE_TYPES = ("Bereavement", "Jury Duty", "Work from Home")
# How many requests the unmoved-balance detail names before "and N more".
UNMOVED_NAMED = 5


def _costs_nothing(kind: str, days, hours) -> bool:
    """Whether a request is worth zero days, so it could never move a balance (#401).

    A leave on a closed-office day (Civic Holiday) is approved with 0 days; it is
    not a balance that failed to move.

    Args:
        kind: The request kind.
        days: The request's days (leave, carry-over/payout), or None.
        hours: The request's hours (overtime), or None.

    Returns:
        True when the amount is recorded and is zero. An unrecorded amount is
        not zero: it stays flagged, since nothing says it cost nothing.
    """
    amount = hours if kind == "overtime" else days                 # overtime is entered in hours
    return amount is not None and float(amount) == 0


async def approved_requests_without_balance_change(employee_id: str | None = None) -> dict[str, list[dict]]:
    """Recent approved requests whose balance never moved, by staff id.

    Only requests decided on or after the legacy cut-off count: older ones
    predate the current balance rules, so a missing move says nothing about
    them. A request counts when it is approved, its balance state is still
    ``not_applied`` (it should have moved a pot and did not), its leave type is
    one that costs days, and it is worth more than zero days.

    Args:
        employee_id: Only this person's requests, or None for everyone.

    Returns:
        Staff id -> that person's requests, each {"kind", "item_id",
        "leave_type", "decided" (a date), "start_date", "end_date", "days",
        "hours", "decided_by"}, newest first. The extra fields feed the settle
        dialog (#400).
    """
    from app.models.request_record import RequestRecord          # lazy: keeps this module's imports light
    from app.services.request_import import LEGACY_CUTOFF          # lazy: the importer imports the staff store
    when = func.coalesce(RequestRecord.decided_at, RequestRecord.created_at)   # old rows may lack a decided date
    query = (
        select(RequestRecord.employee_id, RequestRecord.kind, RequestRecord.item_id,
               RequestRecord.leave_type, when, RequestRecord.start_date, RequestRecord.end_date,
               RequestRecord.days, RequestRecord.hours, RequestRecord.decided_by)
        .where(
            RequestRecord.status == "approved",
            RequestRecord.balance_state == "not_applied",          # should have moved a pot; nothing recorded
            or_(RequestRecord.leave_type.is_(None),                # overtime and carry-over carry no leave type
                RequestRecord.leave_type.not_in(UNMOVED_SKIP_LEAVE_TYPES)),
            RequestRecord.employee_id.is_not(None),
        )
        .order_by(when.desc())
    )
    if employee_id is not None:                                    # the single-employee check: one person only
        query = query.where(RequestRecord.employee_id == str(employee_id))
    async with async_session() as session:
        rows = (await session.execute(query)).all()
    by_person: dict[str, list[dict]] = {}
    for person, kind, item_id, leave_type, decided, start, end, days, hours, decided_by in rows:
        # The cut-off is applied here, on the date alone, so it reads the same on
        # Postgres and SQLite (their stored datetimes compare differently).
        if decided is None or decided.date() < LEGACY_CUTOFF:
            continue                                               # undated, or before the current rules
        if _costs_nothing(kind, days, hours):
            continue                                               # 0 days: nothing to move (#401)
        by_person.setdefault(str(person), []).append({
            "kind": kind, "item_id": item_id, "leave_type": leave_type,
            "decided": decided.date() if decided else None,
            "start_date": start, "end_date": end,
            "days": float(days) if days is not None else None,
            "hours": float(hours) if hours is not None else None,
            "decided_by": decided_by,
        })
    return by_person


def _unmoved_check(unmoved: list[dict]) -> dict:
    """The setup row for approved requests that never moved a balance.

    Args:
        unmoved: The person's requests from approved_requests_without_balance_change.

    Returns:
        One failing check naming the first few requests.
    """
    labels = []
    for r in unmoved[:UNMOVED_NAMED]:
        what = r["leave_type"] or ("overtime" if r["kind"] == "overtime" else "carry-over or payout")
        day = r["decided"]                                        # a date, or None
        when = f", {day:%b} {day.day}, {day.year}" if day else "" # "Sep 29, 2026", portable (no %-d)
        labels.append(f"{r['kind'].replace('_', ' ')} #{r['item_id']} ({what}{when})")
    more = len(unmoved) - UNMOVED_NAMED
    return _check(
        "approved_balance_unmoved", "balances", "fail",
        f"{len(unmoved)} approved request(s) never moved a balance: "
        + "; ".join(labels) + (f"; and {more} more." if more > 0 else "."),
    )


def build_setup_row(
    *,
    record: dict,
    staff_by_name: dict[str, dict],
    name_counts: dict[str, int],
    directory: dict[str, int] | None,
    sp_user_to_name: dict[int, str],
    holidays_by_province: dict[str, list[dict]],
    sample_start: date,
    sample_end: date,
    today: date,
    unmoved: list[dict] | None = None,
) -> dict:
    """Grade one Staff Directory record from already-fetched data. No I/O.

    Runs the same pure core the per-employee check runs, then keeps only the
    checks that grade the record itself. Nothing is re-graded here: whether a
    gap blocks a request or is merely worth a look is decided in one place, by
    the core.

    Args:
        record: The Staff Directory row to grade.
        staff_by_name: Trimmed lowercased name -> Staff Directory row.
        name_counts: Exact Title -> how many records carry it.
        directory: Lowercased email -> lookup id, or None when the Microsoft 365
            directory could not be read.
        sp_user_to_name: Lookup id -> display name.
        holidays_by_province: Province -> its holiday field dicts.
        sample_start: First day of the sample range for the day calculation.
        sample_end: Last day of that range.
        today: The date the sweep is running.
        unmoved: This person's approved requests that never moved a balance
            (see ``approved_requests_without_balance_change``), or None.

    Returns:
        {"employee_id", "employee_name", "department", "location", "fails",
        "warns", "creator_email", "record_url"} - fails is what makes a record
        flagged; warns ride along as context. The last two identify who to tell
        and what to point them at, and are None when the item does not say.
    """
    fields = record.get("fields", {})
    employee_id = str(record.get("id", ""))
    name = fields.get("Title", "")

    if directory is None:
        identity = _identity_unverified()
    else:
        identity = _identity_from_maps(
            employee_id, fields, directory, sp_user_to_name, staff_by_name,
        )

    managers = _resolve_managers_from_map(fields, staff_by_name)

    province: str | None = None
    province_error: str | None = None
    try:
        province = map_location_to_province(fields.get("Location", ""))
    except ValueError as e:
        province_error = str(e)

    report = build_validation_report(
        employee_id=employee_id,
        employee_name=name,
        employee_fields=fields,
        managers=managers,
        all_managers_count=_count_all_managers(fields),
        manager_m365_matches=_count_manager_m365_matches(managers, directory),
        # Every other record carrying this exact name, the record itself aside.
        same_name_others=max(name_counts.get(name, 1) - 1, 0) if name else 0,
        identity=identity,
        province=province,
        province_error=province_error,
        holidays=holidays_by_province.get(province, []) if province else [],
        # The sweep grades the record, not the requests hanging off it.
        requests=[],
        sample_start=sample_start,
        sample_end=sample_end,
        today=today,
    )

    kept = [c for c in report["checks"] if c["category"] in SETUP_LIST_CATEGORIES]
    if unmoved:                                                    # approved, should have moved a pot, did not
        kept.append(_unmoved_check(unmoved))
    return {
        "employee_id": employee_id,
        "employee_name": name,
        "department": fields.get("Department", ""),
        "location": fields.get("Location", ""),
        "fails": [_setup_problem(c) for c in kept if c["status"] == "fail"],
        "warns": [_setup_problem(c) for c in kept if c["status"] == "warn"],
        # The requests behind the unmoved flag, for the Settle requests action (#400).
        "unmoved_requests": unmoved or [],
        # Who to tell about a broken record, and where to send them to fix it.
        "creator_email": _creator_email(record),
        "record_url": record.get("webUrl") or None,
    }


async def validate_all_employee_setups() -> dict:
    """Grade every Staff Directory record, in three reads. Zero writes.

    The read count is fixed no matter the headcount: the Staff Directory, the
    Microsoft 365 User Information List and the holiday list are each fetched
    once, and every per-record lookup the per-employee check would make with a
    call is answered from those in memory.

    Returns:
        {"total_checked", "flagged", "directory_unreadable"} - flagged holds one
        row per record with at least one failing check, sorted by name.
    """
    staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    holiday_items = await get_holiday_repository().get_all()

    # A directory that could not be read stays None all the way down, so the
    # identity and supervisor-email checks report unverified rather than failing
    # every record at once.
    directory: dict[str, int] | None = None
    sp_user_to_name: dict[int, str] = {}
    if await staff_in_postgres():
        # After the switch the staff table is the directory; SharePoint is never read.
        directory, sp_user_to_name = _staff_as_directory(staff)
    else:
        m365_rows = await _fetch_m365_rows()
        if m365_rows is not None:
            directory, sp_user_to_name = _parse_m365_rows(m365_rows)

    staff_by_name = _index_staff_by_name(staff)
    name_counts = _count_staff_names(staff)
    holidays_by_province = _group_holidays_by_province(holiday_items)
    sample_start, sample_end = _sample_weekday_range()
    today = date.today()
    unmoved_by_person = await approved_requests_without_balance_change()   # one read for everyone

    flagged = [
        row for row in (
            build_setup_row(
                record=record,
                staff_by_name=staff_by_name,
                name_counts=name_counts,
                directory=directory,
                sp_user_to_name=sp_user_to_name,
                holidays_by_province=holidays_by_province,
                sample_start=sample_start,
                sample_end=sample_end,
                today=today,
                unmoved=unmoved_by_person.get(str(record.get("id", ""))),
            )
            for record in staff
        )
        if row["fails"]
    ]
    flagged.sort(key=lambda row: (row["employee_name"] or "").lower())

    logger.info(
        "Employee setup sweep: checked %s record(s), flagged %s",
        len(staff), len(flagged),
    )
    return {
        "total_checked": len(staff),
        "flagged": flagged,
        "directory_unreadable": directory is None,               # None only when the user list could not be read
    }
