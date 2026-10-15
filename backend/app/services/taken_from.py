"""Say which balance pots each request's days came out of, or went into (#296).

Every request row the dashboards receive carries a ``taken_from`` object so the
frontend can show a "Taken from" column: for an approved request, the real
balance movements read back from its audit log; for a pending one, the movements
it would make if approved today; and an explicit "nothing moved" for the kinds
that cost no balance (bereavement, jury duty, an hourly worker's non-sick leave)
or that were rejected or cancelled.

The money math is never re-implemented here. Approved deltas come from
``audit_trail.extract_approval_deltas`` (the net of the last approve entry), and
pending projections come from the same ``simulate_*`` functions the request
preview and the dashboards already use, so this view cannot drift from the real
deduction.

Split like the rest of the balance code: ``taken_from_for`` is pure (no I/O, easy
to test), and ``attach_taken_from`` is the thin wrapper that resolves each row's
employee once and stamps the key onto a list of rows.
"""

import logging
from datetime import date

from app.services.audit_trail import extract_approval_deltas
from app.services.employment import (
    VACATION,
    floor_warning,
    is_hourly_bc,
    leave_costs_balance,
    overtime_costs_balance,
)
from app.services.balance import (
    is_next_year_request,
    simulate_carryover_payout_impact,
    simulate_leave_impact,
    simulate_overtime_impact,
)

logger = logging.getLogger(__name__)

# Staff Directory balance column -> wire pot name. These are the five pots a
# request can move; the wire uses snake_case, SharePoint its own column names.
POT_FOR_FIELD = {
    "CurrentSickDayBalance": "sick",       # sick / personal day pot
    "CurrentOvertimeBalance": "make_up",   # make-up / overtime pot
    "CarryOver": "carry_over",             # carried-over vacation
    "CurrentVacationBalance": "vacation",  # main vacation pot
    "Payout": "payout",                    # banked for payout
}
FIELD_FOR_POT = {pot: field for field, pot in POT_FOR_FIELD.items()}  # reverse map

# The order pieces are listed in: the spending cascade, then payout last. The
# frontend shows them in this order, so "where it came from" reads top to bottom.
POT_ORDER = ["sick", "make_up", "carry_over", "vacation", "payout"]

# Only these two pots name a year: a carry-over or payout belongs to the year it
# was earned (see _earned_year); every other pot's year is null.
YEAR_POTS = {"carry_over", "payout"}

# The balance keys we read deltas for; includes Payout, which the default in
# extract_approval_deltas omits, because carry-over/payout approvals move it.
BALANCE_KEYS = (
    "CurrentVacationBalance",
    "CurrentSickDayBalance",
    "CurrentOvertimeBalance",
    "CarryOver",
    "Payout",
)


def _num(value) -> float:
    """One stored balance value as a float, blank or unparseable counting as zero.

    Args:
        value: A SharePoint column value (number, numeric string, None or blank).

    Returns:
        The value as a float, or 0.0 when it is missing or cannot be read.
    """
    try:
        return float(value or 0)          # None/""/0 all collapse to 0.0
    except (TypeError, ValueError):
        return 0.0                        # a stray value never crashes the row


def _parse_day(value) -> date | None:
    """The calendar day out of an ISO date/dateTime string, else None.

    Args:
        value: A date or dateTime string (``YYYY-MM-DD`` or with a time), or None.

    Returns:
        The day as a ``date``, or None when the value is blank or not parseable.
    """
    if not value:                         # missing or blank column
        return None
    try:
        return date.fromisoformat(str(value)[:10])  # first ten chars are the day
    except ValueError:                    # a non-ISO value is simply unknown
        return None


def _earned_year(day: date) -> int:
    """The year a carry-over or payout belongs to, by when the change happened.

    A carry-over stays usable until the April 1 reset, so a balance change dated
    between January 1 and March 31 of year Y is still last year's carry-over and
    reads as ``Y - 1``; from April 1 on it is the new year's, ``Y``. The same
    boundary is applied to a payout, so the two pots label consistently.

    Args:
        day: The date the balance change happened (approval date, or today for a
            pending projection).

    Returns:
        The earned year as an int.
    """
    return day.year - 1 if day.month <= 3 else day.year  # Jan-Mar -> last year


def _change_day(item_fields: dict, today: date) -> date:
    """The date an approved/refunded request's balance change happened.

    Prefers ApprovedDate, then the item's Modified, then Created; falls back to
    today when none is present, so the year rule always has a date to work from.

    Args:
        item_fields: The request's SharePoint field values.
        today: Today in Toronto, the last-resort date.

    Returns:
        The best available change date.
    """
    for key in ("ApprovedDate", "Modified", "Created"):  # most to least specific
        day = _parse_day(item_fields.get(key))
        if day:
            return day
    return today                           # nothing dated: treat as happening today


def _costs_balance(item_fields: dict, request_type: str, employee_fields: dict | None) -> bool:
    """Whether approving this request moves any balance at all.

    Args:
        item_fields: The request's SharePoint field values.
        request_type: "leave", "overtime" or "carryover-payout".
        employee_fields: The submitter's Staff Directory fields, or None when the
            person could not be resolved (then hourly cannot be detected and we
            assume it costs, leaving the audit log or projection to tell the truth).

    Returns:
        True when a balance pot moves; False for the no-cost cases.
    """
    # The per-type rules (salaried, Ontario hourly, BC hourly) live in
    # services/employment.py; unknown person -> the salaried rule.
    if request_type == "leave":
        return leave_costs_balance(employee_fields, item_fields.get("LeaveType", ""))
    if request_type == "overtime":
        return overtime_costs_balance(employee_fields)  # hourly overtime changes no balance
    return True                                         # carry-over / payout always move balances


def _simulate(item_fields: dict, request_type: str, employee_fields: dict) -> dict | None:
    """Projected SharePoint-style balances if this pending request were approved.

    Delegates to the same ``simulate_*`` functions the preview and dashboards use,
    so the projection cannot diverge from the real deduction.

    Args:
        item_fields: The request's SharePoint field values.
        request_type: "leave", "overtime" or "carryover-payout".
        employee_fields: The submitter's current Staff Directory fields.

    Returns:
        The projected balances dict, or None when the request would move nothing
        (a no-impact leave type) or be auto-rejected (vacation would go negative).
    """
    if request_type == "leave":
        leave_type = item_fields.get("LeaveType", "")
        days = _num(item_fields.get("Days"))
        return simulate_leave_impact(employee_fields, leave_type, days, _is_next_year(item_fields))
    if request_type == "overtime":
        return simulate_overtime_impact(employee_fields, _num(item_fields.get("Hours")))
    # carry-over / payout
    return simulate_carryover_payout_impact(
        employee_fields, _num(item_fields.get("Days")), item_fields.get("TypeofRequest", ""),
    )


def _is_next_year(item_fields: dict) -> bool:
    """Whether a leave request's dates fall in next year (changes the cascade).

    Args:
        item_fields: The request's SharePoint field values.

    Returns:
        True when the start or end date is next year; False otherwise or when the
        dates are missing.
    """
    start = _parse_day(item_fields.get("StartDate"))
    end = _parse_day(item_fields.get("EndDate"))
    if start and end:
        return is_next_year_request(start, end)
    return False


def _projected_deltas(
    item_fields: dict, request_type: str, employee_fields: dict, projected: dict | None = None,
) -> dict[str, float]:
    """Per-pot change a pending request would make: projected minus current.

    Args:
        item_fields: The request's SharePoint field values.
        request_type: "leave", "overtime" or "carryover-payout".
        employee_fields: The submitter's current Staff Directory fields.
        projected: The already simulated balances, when the caller has them.

    Returns:
        ``{SharePoint balance key: signed delta}`` (negative = taken out), empty
        when the request would move nothing.
    """
    if projected is None:                               # simulate here unless handed in
        projected = _simulate(item_fields, request_type, employee_fields)
    if projected is None:                               # no-impact or auto-reject
        return {}
    current = {key: _num(employee_fields.get(key)) for key in BALANCE_KEYS}
    # simulate_* for leave/overtime omit Payout, so default it to the current
    # value, leaving its delta at zero.
    return {key: projected.get(key, current[key]) - current[key] for key in BALANCE_KEYS}


def _pieces_from_deltas(deltas: dict[str, float], change_day: date) -> list[dict]:
    """Turn per-pot deltas into ordered, rounded, non-zero pieces.

    Args:
        deltas: ``{SharePoint balance key: signed delta}``.
        change_day: The date used for the carry-over/payout year.

    Returns:
        Pieces in spending order (sick, make_up, carry_over, vacation, payout),
        each ``{"pot", "days": signed float, "year": int or None}``; zero pieces
        are dropped and days are rounded to three decimals.
    """
    pieces: list[dict] = []
    for pot in POT_ORDER:                               # fixed display order
        days = round(_num(deltas.get(FIELD_FOR_POT[pot])), 3)  # signed, rounded
        if days == 0:                                   # nothing moved in this pot
            continue
        year = _earned_year(change_day) if pot in YEAR_POTS else None  # only CO/PO name a year
        pieces.append({"pot": pot, "days": days, "year": year})
    return pieces


def _is_counted(item_fields: dict, request_type: str, employee_fields: dict | None, status: str) -> bool:
    """Whether this row is a BC hourly vacation that only adds to the counter.

    An approval from before #300 (when BC hourly were run as salaried) still has
    an audit log of real movements, and keeps showing those instead.

    Args:
        item_fields: The request's SharePoint field values.
        request_type: "leave", "overtime" or "carryover-payout".
        employee_fields: The submitter's Staff Directory fields, or None.
        status: The request's trimmed Status.

    Returns:
        True for a pending or approved BC hourly vacation with no recorded movement.
    """
    if request_type != "leave" or not is_hourly_bc(employee_fields):
        return False
    if item_fields.get("LeaveType", "") != VACATION or status not in ("Pending", "Approved"):
        return False
    recorded = extract_approval_deltas(item_fields.get("BalanceAuditLog", "") or "", BALANCE_KEYS)
    return recorded is None                            # an old salaried-style approval shows its real deltas


def _none() -> dict:
    """The empty result: nothing was (or would be) taken from any pot."""
    return {"state": "none", "pieces": []}


def taken_from_for(
    item_fields: dict, request_type: str, employee_fields: dict | None, today: date,
) -> dict:
    """Which pots this one request's days came out of, or went into.

    Pure (no I/O): the submitter's balances are passed in. The state tells the
    frontend how to read the pieces: ``recorded`` (approved, from the audit log),
    ``projected`` (pending, if approved today), ``refunded`` (the reversed
    approval's original deltas), ``none`` (cost nothing, or rejected/cancelled),
    or ``unrecorded`` (approved but no usable audit log, e.g. an old request).

    Args:
        item_fields: The request's SharePoint field values (Status, LeaveType,
            Days, BalanceAuditLog, the dates, etc.).
        request_type: "leave", "overtime" or "carryover-payout".
        employee_fields: The submitter's current Staff Directory fields, or None
            when the person could not be resolved.
        today: Today in Toronto, used for a pending request's year and as the
            fallback change date.

    Returns:
        ``{"state": str, "pieces": [{"pot", "days", "year"}, ...]}``.
    """
    status = (item_fields.get("Status") or "").strip()

    if status in ("Rejected", "Cancelled"):            # a decision that moved nothing
        return _none()
    if _is_counted(item_fields, request_type, employee_fields, status):
        # BC hourly vacation (#300): added to the days-taken counter, no pot moves.
        return {"state": "counted", "pieces": [], "counted_days": round(_num(item_fields.get("Days")), 3)}
    # A no-cost kind moved nothing, unless it was approved under an older rule
    # that did move balances (BC hourly before #300): then show what it did.
    moved_before = status in ("Approved", "Refunded") and extract_approval_deltas(
        item_fields.get("BalanceAuditLog", "") or "", BALANCE_KEYS,
    ) is not None
    if not _costs_balance(item_fields, request_type, employee_fields) and not moved_before:
        return _none()

    if status == "Refunded":
        # The reversal restored the balances; show what the original approval did
        # (the last approve entry), so the row still reads as its real movements.
        deltas = extract_approval_deltas(item_fields.get("BalanceAuditLog", "") or "", BALANCE_KEYS)
        pieces = _pieces_from_deltas(deltas or {}, _change_day(item_fields, today))
        return {"state": "refunded", "pieces": pieces}

    if status == "Approved":
        deltas = extract_approval_deltas(item_fields.get("BalanceAuditLog", "") or "", BALANCE_KEYS)
        if deltas is None:                             # approved before audit logs, or none written
            return {"state": "unrecorded", "pieces": []}
        return {"state": "recorded", "pieces": _pieces_from_deltas(deltas, _change_day(item_fields, today))}

    if status == "Pending":
        if employee_fields is None:                    # cannot project without balances
            return {"state": "unrecorded", "pieces": []}
        projected = _simulate(item_fields, request_type, employee_fields)
        deltas = _projected_deltas(item_fields, request_type, employee_fields, projected)
        return {
            "state": "projected",
            "pieces": _pieces_from_deltas(deltas, today),
            # Set when approving takes the floor (sick for BC hourly, vacation
            # otherwise) below zero, so the chip can turn red (#300).
            "below_zero": floor_warning(employee_fields, projected),
        }

    return _none()                                     # unknown/blank status: nothing to attribute


async def attach_taken_from(
    rows: list[dict],
    *,
    employee_fields: dict | None = None,
    staff_by_name: dict | None = None,
    today: date | None = None,
) -> list[dict]:
    """Stamp ``taken_from`` onto each request row, in place.

    Resolves each row's employee once so a pending projection reads real
    balances, without a per-row SharePoint call: callers that already hold the
    staff maps pass ``staff_by_name``; the single-employee endpoints pass
    ``employee_fields``; otherwise one Staff Directory read builds the name map.
    A row that cannot be computed never fails the response: it is logged at debug
    and given state ``unrecorded``.

    Args:
        rows: Request rows, each a dict carrying the SharePoint fields plus
            ``request_type`` and (except the single-employee case) ``employee_name``.
        employee_fields: The one submitter's fields when every row is that person
            (e.g. ``/me/requests``); used for every row and skips any lookup.
        staff_by_name: ``{lowercased name: {"id", "fields"}}`` from the caller's
            own Staff Directory read, reused to resolve each row's employee.
        today: Today in Toronto; defaults to the Toronto date when omitted.

    Returns:
        The same ``rows`` list, each row with a ``taken_from`` key added.
    """
    if not rows:                                       # nothing to do, no reads
        return rows
    if today is None:                                  # default to the Toronto date
        from app.services.app_settings import toronto_today
        today = toronto_today()

    name_to_item = staff_by_name
    if employee_fields is None and name_to_item is None:
        # Self-sufficient fallback: one read, built into a name -> item map.
        from app.config import settings
        from app.graph.sharepoint import sp_client
        name_to_item = {}
        try:
            for item in await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY):
                name = (item.get("fields", {}).get("Title") or "").strip().lower()
                if name:
                    name_to_item[name] = item
        except Exception:  # noqa: BLE001 - a failed read only weakens projections
            logger.exception("taken_from: could not load the Staff Directory")
            name_to_item = {}

    for row in rows:
        try:
            emp_fields = employee_fields                # the single-employee shortcut
            if emp_fields is None:                      # resolve by the row's employee_name
                name = (row.get("employee_name") or "").strip().lower()
                item = name_to_item.get(name) if (name and name_to_item) else None
                emp_fields = item.get("fields") if item else None
            # The row itself carries the SharePoint fields (merged in by the
            # endpoint), so it doubles as item_fields for the pure function.
            row["taken_from"] = taken_from_for(row, row.get("request_type", ""), emp_fields, today)
        except Exception:  # noqa: BLE001 - one bad row must not fail the whole response
            logger.debug("taken_from: could not compute for a %s row", row.get("request_type"), exc_info=True)
            row["taken_from"] = {"state": "unrecorded", "pieces": []}
    return rows
