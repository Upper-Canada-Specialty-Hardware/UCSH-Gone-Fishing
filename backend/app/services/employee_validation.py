"""Read-only "does this employee's setup work?" validation suite (GH #41).

Runs an employee's CURRENT Staff Directory values through every request
workflow — identity resolution, supervisor/manager lookup, location -> province
-> holiday calendar, and a pure balance simulation for each leave / overtime /
carryover-payout type — WITHOUT creating any request or sending any
notification.

Why this exists: previously the only way to confirm an employee was set up
correctly (e.g. "is their supervisor linked?") was to fire a REAL leave request
in their name, which notified uninvolved employees. This suite reproduces every
check that a real request would exercise, using only reads and the balance
engine's pure `simulate_*` functions.

SAFETY CONTRACT: this module performs READS ONLY. It must never call
`create_list_item`, `update_list_item_fields`, `send_email`,
`send_email_with_dashboard`, `send_approval_email`, or `send_sms`. A guard test
(`tests/test_employee_validation.py`) enforces that it stays side-effect free —
keep it that way.

The suite is split like the balance engine: `build_validation_report(...)` is a
pure function over already-fetched inputs (trivially unit-testable), and
`validate_employee_setup(...)` is the thin async wrapper that does the SharePoint
reads and feeds the pure core.
"""

import logging
from datetime import date, timedelta

from app.services.employee import (
    get_all_managers_for_employee,
    get_employee_by_id,
    map_location_to_province,
    resolve_person_field,
)
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.holidays import get_holidays_for_province, get_half_friday_season
from app.services.business_days import calculate_business_days
from app.services.balance import (
    simulate_leave_impact,
    simulate_overtime_impact,
    simulate_carryover_payout_impact,
)

logger = logging.getLogger(__name__)

# Representative sample inputs for the dry-run simulations. Nothing is written —
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


def _check(code: str, category: str, status: str, detail: str, projected=None) -> dict:
    """One row of the report. status is 'pass' | 'warn' | 'fail'."""
    return {
        "code": code,
        "category": category,
        "status": status,
        "detail": detail,
        "projected": projected,
    }


def _count_all_managers(fields: dict) -> int:
    am = fields.get("AllManagers")
    return len(am) if isinstance(am, list) else 0


def _sample_weekday_range() -> tuple[date, date]:
    """Next Monday..Tuesday — a stable 1-business-day range for the calc check."""
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
    identity: dict,
    province: str | None,
    province_error: str | None,
    holidays: list[dict],
    sample_start: date,
    sample_end: date,
) -> dict:
    """Pure core: assemble the full report from already-fetched inputs.

    `identity` is the pre-computed round-trip result {"status", "detail"};
    `managers` is the list already resolved via get_all_managers_for_employee;
    `province`/`province_error` come from map_location_to_province in the wrapper;
    `holidays` is the list already fetched for that province.
    """
    checks: list[dict] = []

    # --- Identity (email -> M365 user -> Staff Directory round-trip) ---
    checks.append(_check(
        "identity_roundtrip", "identity", identity["status"], identity["detail"],
    ))

    # --- Supervisor / AllManagers ---
    if all_managers_count == 0:
        checks.append(_check(
            "supervisor_set", "supervisor", "fail",
            "AllManagers is empty on the Staff Directory record — a request would "
            "have no manager to route approval to.",
        ))
    else:
        checks.append(_check(
            "supervisor_set", "supervisor", "pass",
            f"AllManagers lists {all_managers_count} manager(s).",
        ))
        # Every listed manager must resolve to a real Staff Directory employee
        # via the SAME path the approval email uses.
        if len(managers) < all_managers_count:
            checks.append(_check(
                "supervisor_resolves", "supervisor", "fail",
                f"Only {len(managers)} of {all_managers_count} listed manager(s) "
                "resolved to a Staff Directory employee (name/LookupValue mismatch) "
                "— approval routing would be incomplete.",
            ))
        else:
            checks.append(_check(
                "supervisor_resolves", "supervisor", "pass",
                f"All {all_managers_count} manager(s) resolve: "
                + ", ".join(m.get("fields", {}).get("Title", "?") for m in managers),
            ))
        # Each resolved manager needs an email to actually receive the approval.
        unreachable = [
            m.get("fields", {}).get("Title", "?")
            for m in managers
            if not (m.get("fields", {}).get("EmailAddress") or "").strip()
        ]
        if unreachable:
            checks.append(_check(
                "manager_reachable", "supervisor", "warn",
                "Manager(s) with no Email Address (approval email can't reach them): "
                + ", ".join(unreachable),
            ))
        elif managers:
            checks.append(_check(
                "manager_reachable", "supervisor", "pass",
                "All resolved managers have an Email Address.",
            ))

    # --- Location -> province ---
    if province_error:
        checks.append(_check(
            "location_province", "location", "fail",
            f"{province_error} — auto-calculation of Days would raise and the "
            "request would silently get stuck.",
        ))
    else:
        checks.append(_check(
            "location_province", "location", "pass",
            f"Location '{employee_fields.get('Location', '')}' maps to province {province}.",
        ))

    # --- Holiday calendar for the province ---
    if province and not province_error:
        if holidays:
            season = get_half_friday_season(holidays)
            season_note = (
                " Half-Friday season detected." if season[0] and season[1]
                else " (No half-Friday season rows found for this province.)"
            )
            checks.append(_check(
                "holidays_load", "location", "pass",
                f"{len(holidays)} holiday row(s) loaded for {province}.{season_note}",
            ))
        else:
            checks.append(_check(
                "holidays_load", "location", "warn",
                f"No holiday rows found for province {province} — business-day math "
                "will treat every weekday as workable.",
            ))

    # --- Balance pots are numeric ---
    bad_pots = []
    for pot in BALANCE_POTS:
        raw = employee_fields.get(pot)
        try:
            float(raw or 0)
        except (TypeError, ValueError):
            bad_pots.append(f"{pot}={raw!r}")
    if bad_pots:
        checks.append(_check(
            "balances_numeric", "balances", "fail",
            "Non-numeric balance value(s): " + ", ".join(bad_pots)
            + " — every balance simulation would crash on this record.",
        ))
    else:
        checks.append(_check(
            "balances_numeric", "balances", "pass",
            "All five balance pots are numeric: "
            + ", ".join(f"{p}={float(employee_fields.get(p) or 0)}" for p in BALANCE_POTS),
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
        try:
            season = get_half_friday_season(holidays)
            days = calculate_business_days(sample_start, sample_end, holidays, season)
            checks.append(_check(
                "sim_business_days", "simulation", "pass",
                f"Business-day calc over {sample_start}..{sample_end} = {days} day(s).",
            ))
        except Exception as e:  # noqa: BLE001 - report any failure as a red check
            checks.append(_check(
                "sim_business_days", "simulation", "fail",
                f"Business-day calculation raised: {e}",
            ))

    # --- Overtime dry run ---
    try:
        projected = simulate_overtime_impact(employee_fields, SAMPLE_OVERTIME_HOURS)
        checks.append(_check(
            "sim_overtime", "simulation", "pass",
            f"Overtime of {SAMPLE_OVERTIME_HOURS}h simulates cleanly "
            f"(+{SAMPLE_OVERTIME_HOURS / 8} make-up days, with vacation offset).",
            projected,
        ))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("sim_overtime", "simulation", "fail", f"Overtime simulation raised: {e}"))

    # --- Carryover & Payout dry runs ---
    for code, req_type in [("sim_carry_over", "Carry Over"), ("sim_payout", "Payout")]:
        checks.append(_simulate_co_po_case(code, req_type, employee_fields))

    return {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "overall": _overall(checks),
        "checks": checks,
    }


def _simulate_leave_case(code, leave_type, fields, days, is_next_year) -> dict:
    try:
        projected = simulate_leave_impact(fields, leave_type, days, is_next_year)
        if projected is None:
            return _check(
                code, "simulation", "pass",
                f"{leave_type}: no balance impact (correctly handled as a no-cost type).",
            )
        label = f"{leave_type}{' (next-year)' if is_next_year else ''}"
        return _check(code, "simulation", "pass", f"{label}: {days} day(s) simulates cleanly.", projected)
    except Exception as e:  # noqa: BLE001
        return _check(code, "simulation", "fail", f"{leave_type} simulation raised: {e}")


def _simulate_co_po_case(code, req_type, fields) -> dict:
    try:
        projected = simulate_carryover_payout_impact(fields, SAMPLE_CO_PO_DAYS, req_type)
        if projected is None:
            return _check(
                code, "simulation", "warn",
                f"{req_type} of {SAMPLE_CO_PO_DAYS} day(s) would be system-rejected "
                "(vacation would go negative) with the current balance. Valid outcome, "
                "but worth noting if the employee expects to move days.",
            )
        return _check(code, "simulation", "pass", f"{req_type} of {SAMPLE_CO_PO_DAYS} day(s) simulates cleanly.", projected)
    except Exception as e:  # noqa: BLE001
        return _check(code, "simulation", "fail", f"{req_type} simulation raised: {e}")


# --- Thin async wrapper: SharePoint reads, then hand off to the pure core ---

async def _check_identity(employee_id, fields: dict) -> dict:
    """Reproduce the email -> M365 user -> Staff Directory round-trip a real
    request relies on, and confirm it lands back on THIS employee."""
    email = (fields.get("EmailAddress") or "").strip()
    if not email:
        return {"status": "fail", "detail": (
            "No Email Address on the Staff Directory record — a submitted request "
            "cannot be linked back to this person."
        )}
    lookup_id = await _resolve_user_lookup_id(email)
    if not lookup_id:
        return {"status": "fail", "detail": (
            f"Email {email} is not in the M365 User Information List — a request "
            "submitted by this person would not resolve to their record."
        )}
    resolved = await resolve_person_field(lookup_id)
    if not resolved:
        return {"status": "fail", "detail": (
            "The M365 identity for this email did not resolve to any Staff Directory "
            "record (display name likely differs from the Staff Directory Title)."
        )}
    if str(resolved.get("id")) != str(employee_id):
        other = resolved.get("fields", {}).get("Title", "")
        return {"status": "fail", "detail": (
            f"Identity resolves to a DIFFERENT Staff Directory record "
            f"(#{resolved.get('id')} {other}), not this one — names/emails are crossed."
        )}
    return {"status": "pass", "detail": (
        f"Email -> M365 user -> Staff Directory round-trip resolves correctly to "
        f"{resolved.get('fields', {}).get('Title', '')}."
    )}


async def validate_employee_setup(employee_id: str | int) -> dict:
    """Run the full read-only validation suite for one employee. Zero writes."""
    employee = await get_employee_by_id(employee_id)
    if not employee:
        return {
            "employee_id": str(employee_id),
            "employee_name": "",
            "overall": "fail",
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
        identity=identity,
        province=province,
        province_error=province_error,
        holidays=holidays,
        sample_start=sample_start,
        sample_end=sample_end,
    )
    logger.info(
        "Employee setup validation for #%s (%s): overall=%s",
        employee_id, name, report["overall"],
    )
    return report
