"""Which balance pots each request's days came from, or went into (#296).

Two layers: the pure ``taken_from_for`` against hand-built audit logs and
employee balances (the bulk, since the math lives there), and one presence test
per endpoint family proving the ``taken_from`` key is attached to every row.
"""

import asyncio
import itertools
import json

import pytest
from datetime import date, datetime, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.request_record import RequestRecord
from app.routes import dashboard
from app.routes.dashboard import DashboardUser, _PersonNames, _enrich_pending_item, get_dashboard_user
from app.services.employment import vacation_days_counted
from app.services.taken_from import _earned_year, attach_taken_from, taken_from_for


def _run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


_ledger_item_ids = itertools.count(60_000_000)

# A fixed "today" so the pending-projection year is deterministic: November is
# past April 1, so a carry-over/payout earned now reads as the current year.
TODAY = date(2026, 11, 2)

# A salaried employee with a day in every spending pot, used across the tests.
EMP = {
    "Title": "Pat Staff", "EmailAddress": "pat@ucsh.com", "SalaryHourly": "Salary",
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 5,
    "CurrentOvertimeBalance": 2, "CarryOver": 1, "Payout": 0,
}
HOURLY = {**EMP, "SalaryHourly": "Hourly"}


def _step(op, before, after):
    """One audit step: the operation name and the before/after balance snapshots."""
    return {"operation": op, "before": before, "after": after}


def _log(steps, action="approve", extra=None):
    """A BalanceAuditLog JSON string of one entry (plus an optional later one).

    Args:
        steps: The entry's steps (from _step).
        action: The entry's action, "approve" by default.
        extra: An optional second entry {"action", "steps"} appended after it
            (e.g. a refund entry that follows the approve entry).

    Returns:
        The audit log as a JSON string, as SharePoint stores it.
    """
    entries = [{"action": action, "timestamp": "2026-11-02T10:00:00", "steps": steps}]
    if extra:
        entries.append(extra)
    return json.dumps(entries)


# ---------------------------------------------------------------------------
# Pure function: recorded (approved), from the audit log
# ---------------------------------------------------------------------------

def test_vacation_recorded_spends_make_up_carryover_and_vacation():
    # Vacation, 5 days: Make-Up 2 -> -3, cascade to CarryOver 1 -> 0 and
    # Vacation 10 -> 8. Net: make_up -2, carry_over -1, vacation -2.
    audit = _log([
        _step("Deduct Vacation from Make-Up",
              {"CurrentOvertimeBalance": 2}, {"CurrentOvertimeBalance": -3}),
        _step("Cascade (current year)",
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": -3, "CarryOver": 1, "CurrentVacationBalance": 10},
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": 0, "CarryOver": 0, "CurrentVacationBalance": 8}),
    ])
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "make_up", "days": -2.0, "year": None},
        {"pot": "carry_over", "days": -1.0, "year": 2026},
        {"pot": "vacation", "days": -2.0, "year": None},
    ]


def test_sick_within_balance_recorded():
    audit = _log([
        _step("Deduct Sick/Personal from Sick",
              {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3}),
    ])
    item = {"Status": "Approved", "LeaveType": "Sick or Personal Day", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


def test_next_year_request_makes_carryover_negative_recorded():
    # Next-year vacation, 3 days: Make-Up 1 -> -2, cascade Make-Up -> CarryOver,
    # leaving CarryOver at -2. Net: make_up -1, carry_over -2.
    audit = _log([
        _step("Deduct Vacation from Make-Up",
              {"CurrentOvertimeBalance": 1}, {"CurrentOvertimeBalance": -2}),
        _step("Cascade (next year)",
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": -2, "CarryOver": 0, "CurrentVacationBalance": 10},
              {"CurrentSickDayBalance": 5, "CurrentOvertimeBalance": 0, "CarryOver": -2, "CurrentVacationBalance": 10}),
    ])
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["pieces"] == [
        {"pot": "make_up", "days": -1.0, "year": None},
        {"pot": "carry_over", "days": -2.0, "year": 2026},
    ]


def test_carryover_request_feb_approval_is_previous_year():
    # Carry Over adds to CarryOver; a Feb approval is still last year's carry-over.
    audit = _log([
        _step("Transfer Vacation to Carry Over",
              {"CurrentVacationBalance": 10, "CarryOver": 0}, {"CurrentVacationBalance": 7, "CarryOver": 3}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Carry Over", "ApprovedDate": "2026-02-15", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "carry_over", "days": 3.0, "year": 2025},   # Feb -> previous year
        {"pot": "vacation", "days": -3.0, "year": None},
    ]


def test_carryover_request_may_approval_is_same_year():
    audit = _log([
        _step("Transfer Vacation to Carry Over",
              {"CurrentVacationBalance": 10, "CarryOver": 0}, {"CurrentVacationBalance": 7, "CarryOver": 3}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Carry Over", "ApprovedDate": "2026-05-10", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["pieces"][0] == {"pot": "carry_over", "days": 3.0, "year": 2026}  # May -> same year


def test_payout_recorded_names_the_year():
    audit = _log([
        _step("Transfer Vacation to Payout",
              {"CurrentVacationBalance": 10, "Payout": 0}, {"CurrentVacationBalance": 8, "Payout": 2}),
    ])
    item = {"Status": "Approved", "TypeofRequest": "Payout", "ApprovedDate": "2026-05-10", "BalanceAuditLog": audit}
    result = taken_from_for(item, "carryover-payout", EMP, TODAY)
    assert result["pieces"] == [
        {"pot": "vacation", "days": -2.0, "year": None},
        {"pot": "payout", "days": 2.0, "year": 2026},
    ]


def test_overtime_recorded_adds_make_up_and_pays_back_vacation():
    # 2 days overtime added, then offset against a -1 vacation: net make_up +1,
    # vacation +1.
    audit = _log([
        _step("Add overtime to Make-Up",
              {"CurrentOvertimeBalance": 0}, {"CurrentOvertimeBalance": 2}),
        _step("Vacation offset",
              {"CurrentVacationBalance": -1, "CurrentOvertimeBalance": 2},
              {"CurrentVacationBalance": 0, "CurrentOvertimeBalance": 1}),
    ])
    item = {"Status": "Approved", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "overtime", EMP, TODAY)
    assert result["state"] == "recorded"
    assert result["pieces"] == [
        {"pot": "make_up", "days": 1.0, "year": None},
        {"pot": "vacation", "days": 1.0, "year": None},
    ]


# ---------------------------------------------------------------------------
# Pure function: projected (pending)
# ---------------------------------------------------------------------------

def test_pending_vacation_projected():
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "projected"
    assert result["pieces"] == [
        {"pot": "make_up", "days": -2.0, "year": None},
        {"pot": "carry_over", "days": -1.0, "year": 2026},
        {"pot": "vacation", "days": -2.0, "year": None},
    ]


def test_pending_overtime_projection_rounds_to_three_decimals():
    # 5 hours / 8 = 0.625 of a day into Make-Up; vacation is positive so no offset.
    emp = {**EMP, "CurrentVacationBalance": 5, "CurrentOvertimeBalance": 0}
    item = {"Status": "Pending", "Hours": 5}
    result = taken_from_for(item, "overtime", emp, TODAY)
    assert result["pieces"] == [{"pot": "make_up", "days": 0.625, "year": None}]


def test_pending_hourly_vacation_is_counted():
    # Hourly vacation is a count of days taken, not a balance, wherever they work.
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    assert taken_from_for(item, "leave", HOURLY, TODAY) == {"state": "counted", "pieces": [], "counted_days": 5.0}


# ---------------------------------------------------------------------------
# Pure function: none / unrecorded / refunded
# ---------------------------------------------------------------------------

def test_approved_hourly_vacation_is_counted():
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02", "Days": 2}
    assert taken_from_for(item, "leave", HOURLY, TODAY) == {"state": "counted", "pieces": [], "counted_days": 2.0}


def test_bereavement_is_none():
    item = {"Status": "Approved", "LeaveType": "Bereavement", "ApprovedDate": "2026-11-02"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "none", "pieces": []}


def test_approved_without_audit_is_unrecorded():
    item = {"Status": "Approved", "LeaveType": "Vacation", "ApprovedDate": "2026-11-02"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "unrecorded", "pieces": []}


def test_rejected_is_none():
    item = {"Status": "Rejected", "LeaveType": "Vacation"}
    assert taken_from_for(item, "leave", EMP, TODAY) == {"state": "none", "pieces": []}


def test_refunded_uses_the_original_approval_deltas():
    # An approve entry then a refund entry; the refunded state shows the approve
    # entry's deltas, not the reversal.
    audit = _log(
        [_step("Deduct Sick/Personal from Sick",
               {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3})],
        extra={"action": "refund", "steps": [
            _step("Refund", {"CurrentSickDayBalance": 3}, {"CurrentSickDayBalance": 5})]},
    )
    item = {"Status": "Refunded", "LeaveType": "Sick or Personal Day", "ApprovedDate": "2026-11-02", "BalanceAuditLog": audit}
    result = taken_from_for(item, "leave", EMP, TODAY)
    assert result["state"] == "refunded"
    assert result["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


def test_earned_year_boundary():
    assert _earned_year(date(2026, 1, 1)) == 2025
    assert _earned_year(date(2026, 3, 31)) == 2025     # still last year's carry-over
    assert _earned_year(date(2026, 4, 1)) == 2026      # from April 1, the new year
    assert _earned_year(date(2026, 12, 31)) == 2026


def test_unresolved_employee_on_pending_is_unrecorded():
    item = {"Status": "Pending", "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5}
    assert taken_from_for(item, "leave", None, TODAY) == {"state": "unrecorded", "pieces": []}


# ---------------------------------------------------------------------------
# Endpoints: the key is present on every row
# ---------------------------------------------------------------------------

MANAGER = {"id": "9", "fields": {"Title": "Boss One", "EmailAddress": "boss@ucsh.com"}}
EMP_ITEM = {"id": "7", "fields": {**EMP, "AllManagers": [{"LookupValue": "Boss One"}]}}

# An approved leave (recorded) and a fully-processed pending leave (projected),
# both resolving to Pat Staff through SubmitterEmail.
APPROVED_LEAVE = {"id": "100", "fields": {
    "Status": "Approved", "LeaveType": "Sick or Personal Day", "SubmitterEmail": "pat@ucsh.com",
    "ApprovedDate": "2026-11-02", "Days": 2,
    "BalanceAuditLog": _log([_step("Deduct Sick/Personal from Sick",
                                   {"CurrentSickDayBalance": 5}, {"CurrentSickDayBalance": 3})]),
}}
PENDING_LEAVE = {"id": "101", "fields": {
    "Status": "Pending", "LeaveType": "Vacation", "SubmitterEmail": "pat@ucsh.com",
    "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5, "ManagerLookupId": 55,
}}


def _lookups():
    """The four maps _build_staff_lookups returns, built for Pat Staff."""
    sp_user_to_name = _PersonNames()
    sp_user_to_name.by_email["pat@ucsh.com"] = "Pat Staff"
    return (
        {"pat staff": EMP_ITEM},            # staff_by_name (lowercased)
        {7: EMP_ITEM},                      # staff_by_id
        sp_user_to_name,                    # sp_user_to_name (+ by_email)
        {"boss one": {"Pat Staff"}},        # mgr_to_emp_names, keyed by normalised name (#376)
    )


@pytest.fixture
def mounted(monkeypatch):
    """The dashboard router with the Staff Directory and request lists stubbed.

    Returns a factory: ``mounted(role, uid, leave_items)`` gives a TestClient
    whose auth is overridden to that role/uid and whose leave list returns the
    given items (the other two request lists are empty).
    """
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)

    async def fake_lookups():
        return _lookups()

    async def fake_by_id(eid):
        return {"7": EMP_ITEM, "9": MANAGER}.get(str(eid))

    monkeypatch.setattr(dashboard, "_build_staff_lookups", fake_lookups)
    monkeypatch.setattr(dashboard, "get_employee_by_id", fake_by_id)

    def factory(role, uid, leave_items):
        async def fake_get_list_items(list_id, *a, **k):
            return list(leave_items) if list_id == settings.SP_LIST_LEAVE_REQUESTS else []

        monkeypatch.setattr(dashboard.request_store, "get_list_items", fake_get_list_items)

        app = FastAPI()
        app.include_router(dashboard.router)
        app.dependency_overrides[get_dashboard_user] = lambda: DashboardUser(role=role, user_id=uid)
        return TestClient(app, raise_server_exceptions=False)

    return factory


def _states(rows):
    """The set of taken_from states across rows, asserting the key is on each."""
    assert rows, "expected at least one row"
    for r in rows:
        assert "taken_from" in r
        assert r["taken_from"]["state"] in ("recorded", "projected", "none", "unrecorded", "refunded")
    return {r["taken_from"]["state"] for r in rows}


def test_me_requests_carry_taken_from(mounted):
    client = mounted("employee", "7", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/me/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_team_requests_carry_taken_from(mounted):
    client = mounted("manager", "9", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/team/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_team_pending_carry_taken_from(mounted):
    client = mounted("manager", "9", [PENDING_LEAVE])
    resp = client.get("/team/pending")
    assert resp.status_code == 200
    assert _states(resp.json()["pending"]) == {"projected"}


def test_admin_requests_carry_taken_from(mounted):
    client = mounted("admin", "7", [APPROVED_LEAVE, PENDING_LEAVE])
    resp = client.get("/admin/requests")
    assert resp.status_code == 200
    assert _states(resp.json()["requests"]) == {"recorded", "projected"}


def test_admin_pending_carry_taken_from(mounted):
    client = mounted("admin", "7", [PENDING_LEAVE])
    resp = client.get("/admin/pending")
    assert resp.status_code == 200
    assert _states(resp.json()["pending"]) == {"projected"}


def test_admin_stuck_requests_carry_taken_from(mounted, monkeypatch):
    # A pending leave with no manager is "stuck". Its approval-email record reads
    # as unknown, and there is no conflict to find.
    stuck_item = {"id": "102", "fields": {
        "Status": "Pending", "LeaveType": "Vacation", "SubmitterEmail": "pat@ucsh.com",
        "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 0,
    }}

    async def fake_emailed(_entries):
        return None

    monkeypatch.setattr(dashboard, "fetch_approval_email_records", fake_emailed)
    client = mounted("admin", "7", [stuck_item])
    resp = client.get("/admin/stuck-requests")
    assert resp.status_code == 200
    rows = resp.json()["stuck"]
    assert rows and all("taken_from" in r for r in rows)


# ---------------------------------------------------------------------------
# After the switch: a decided request reads taken_from from the ledger (finding 5)
# ---------------------------------------------------------------------------

async def _insert_decided(kind, item_id, status, ledger_rows,
                          balance_state="applied", leave_type="Sick or Personal Day", extra=None):
    """Insert one decided request plus its balance_ledger rows, and return nothing.

    Args:
        kind: The request kind.
        item_id: The item_id people and links use.
        status: The lower-case lifecycle status ("approved" / "refunded").
        ledger_rows: ``[(pot, change, before, after, reason)]`` to insert.
        balance_state: The request's balance_state (the importer's verdict).
        leave_type: The leave type stored on the request.
        extra: The request's extra JSON (the history step's verdict lives there).
    """
    async with async_session() as session:
        record = RequestRecord(
            kind=kind, item_id=item_id, status=status, employee_id="7",
            employee_name="Pat Staff", employee_email="pat@ucsh.com",
            leave_type=leave_type, start_date=date(2026, 11, 2),
            days=2.0, approval_version=1, balance_state=balance_state,
            decided_at=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), extra=extra,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        for pot, change, before, after, reason in ledger_rows:
            session.add(BalanceLedger(
                request_id=record.id, employee_id="7", pot=pot, change=change,
                before=before, after=after, reason=reason, actor="Boss",
                channel="manager_dashboard",
                idempotency_key=f"tk-{item_id}-{pot}-{next(_ledger_item_ids)}",
            ))
        await session.commit()


@pytest.fixture
def _clean_requests():
    """Wipe the request and ledger rows these ledger-path tests insert."""
    async def wipe():
        async with async_session() as session:
            await session.execute(delete(BalanceLedger))
            await session.execute(delete(RequestRecord))
            await session.commit()
    _run(wipe())
    yield
    _run(wipe())


def test_approved_postgres_request_reads_taken_from_from_the_ledger(monkeypatch, _clean_requests):
    # After the switch the decision writes no BalanceAuditLog; its pots are the
    # balance_ledger rows. The row here carries no audit log at all, so the old
    # pure path would read "unrecorded"; the ledger path must read "recorded".
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    item_id = next(_ledger_item_ids)
    _run(_insert_decided("leave", item_id, "approved",
                         [("sick", -2.0, 5.0, 3.0, f"approved leave #{item_id}")]))

    rows = [{"id": str(item_id), "request_type": "leave", "Status": "Approved",
             "employee_name": "Pat Staff"}]       # no BalanceAuditLog key on purpose
    _run(attach_taken_from(rows, employee_fields=EMP, today=TODAY))

    assert rows[0]["taken_from"]["state"] == "recorded"
    assert rows[0]["taken_from"]["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


@pytest.mark.parametrize("leave_type, balance_state, expected", [
    ("Vacation", "not_applied", "unrecorded"),        # imported, audit log missing: unknown, not "nothing"
    ("Vacation", "legacy_unknown", "unrecorded"),     # an old approval: same
    ("Work from Home", "not_applied", "none"),       # the retired type never moved a balance
    ("Bereavement", "no_balance", "none"),           # costs nothing by rule
])
def test_an_imported_approval_with_no_ledger_is_not_recorded_rather_than_free(
    monkeypatch, _clean_requests, leave_type, balance_state, expected,
):
    # An imported SharePoint approval whose audit log was missing has no ledger
    # rows. That means nobody knows which pots moved, so it must read "Not
    # recorded"; "No balance used" is kept for kinds that really cost nothing.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    item_id = next(_ledger_item_ids)
    _run(_insert_decided("leave", item_id, "approved", [], balance_state=balance_state, leave_type=leave_type))

    rows = [{"id": str(item_id), "request_type": "leave", "Status": "Approved",
             "LeaveType": leave_type, "employee_name": "Pat Staff"}]
    _run(attach_taken_from(rows, employee_fields=EMP, today=TODAY))

    assert rows[0]["taken_from"] == {"state": expected, "pieces": []}


def test_an_approval_the_history_could_not_prove_says_why(monkeypatch, _clean_requests):
    # The history step (#357) keeps why it found nothing; the row passes it on
    # so the column can say it plainly instead of a bare "Not recorded".
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    item_id = next(_ledger_item_ids)
    _run(_insert_decided("leave", item_id, "approved", [], balance_state="legacy_unknown", leave_type="Vacation",
                         extra={"sp_history": {"version": "4.0", "balance": "before_records"}}))

    rows = [{"id": str(item_id), "request_type": "leave", "Status": "Approved",
             "LeaveType": "Vacation", "employee_name": "Pat Staff"}]
    _run(attach_taken_from(rows, employee_fields=EMP, today=TODAY))

    assert rows[0]["taken_from"] == {"state": "unrecorded", "pieces": [], "reason": "before_records"}


def test_refunded_postgres_request_still_shows_what_the_approval_took(monkeypatch, _clean_requests):
    # A refund adds reversal rows (reason starts "refund"); they must be ignored
    # so the row still reads "refunded" with the approval's original movement.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    item_id = next(_ledger_item_ids)
    _run(_insert_decided("leave", item_id, "refunded", [
        ("sick", -2.0, 5.0, 3.0, f"approved leave #{item_id}"),
        ("sick", 2.0, 3.0, 5.0, f"refund of leave #{item_id}"),     # the reversal, ignored
    ]))

    rows = [{"id": str(item_id), "request_type": "leave", "Status": "Refunded",
             "employee_name": "Pat Staff"}]
    _run(attach_taken_from(rows, employee_fields=EMP, today=TODAY))

    assert rows[0]["taken_from"]["state"] == "refunded"
    assert rows[0]["taken_from"]["pieces"] == [{"pot": "sick", "days": -2.0, "year": None}]


def test_approved_postgres_hourly_vacation_reads_as_counted(monkeypatch, _clean_requests):
    # An hourly vacation moves no pot; its ledger is a single vacation_taken
    # counter row, which must read as "counted" with the day count.
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    item_id = next(_ledger_item_ids)
    _run(_insert_decided("leave", item_id, "approved",
                         [("vacation_taken", 2.0, 0.0, 2.0, f"approved leave #{item_id}")]))

    rows = [{"id": str(item_id), "request_type": "leave", "Status": "Approved",
             "employee_name": "Pat Staff"}]
    _run(attach_taken_from(rows, employee_fields=HOURLY, today=TODAY))

    assert rows[0]["taken_from"] == {"state": "counted", "pieces": [], "counted_days": 2.0}


def test_many_decided_rows_load_their_ledgers_in_a_few_queries(monkeypatch, _clean_requests):
    # The admin requests list holds thousands of decided rows. Each must still get
    # its own ledger, across kinds, while the reads stay a handful of queries
    # instead of two per row (two per row made that list take over 30 seconds).
    from sqlalchemy import event
    from app.database import engine
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    leave_ids = [next(_ledger_item_ids) for _ in range(5)]
    overtime_id = next(_ledger_item_ids)
    for item_id in leave_ids:                            # five leave rows, one sick day each
        _run(_insert_decided("leave", item_id, "approved",
                             [("sick", -1.0, 5.0, 4.0, f"approved leave #{item_id}")]))
    _run(_insert_decided("overtime", overtime_id, "approved",
                         [("make_up", 0.5, 0.0, 0.5, f"approved overtime #{overtime_id}")]))
    missing_id = next(_ledger_item_ids)                  # no request row behind this one

    rows = [{"id": str(i), "request_type": "leave", "Status": "Approved",
             "employee_name": "Pat Staff"} for i in leave_ids]
    rows.append({"id": str(overtime_id), "request_type": "overtime", "Status": "Approved",
                 "employee_name": "Pat Staff"})
    rows.append({"id": str(missing_id), "request_type": "leave", "Status": "Approved",
                 "employee_name": "Pat Staff"})

    statements = []
    def _count(conn, cursor, statement, *args):          # every SQL statement sent
        statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        _run(attach_taken_from(rows, employee_fields=EMP, today=TODAY))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    for row in rows[:5]:                                 # each leave row reads its own ledger
        assert row["taken_from"]["state"] == "recorded"
        assert row["taken_from"]["pieces"] == [{"pot": "sick", "days": -1.0, "year": None}]
    assert rows[5]["taken_from"]["state"] == "recorded"  # the overtime row, its own kind
    assert rows[5]["taken_from"]["pieces"][0]["pot"] == "make_up"
    assert rows[6]["taken_from"] == {"state": "unrecorded", "pieces": []}
    ledger_reads = [q for q in statements if "balance_ledger" in q or "FROM requests" in q]
    assert len(ledger_reads) <= 3                        # one per kind, one for the ledger


# ---------------------------------------------------------------------------
# The pending projection follows the request's stamped employment type (finding 5)
# ---------------------------------------------------------------------------

def test_pending_enrich_uses_the_requests_stamped_employment_type():
    # The person is salaried NOW, but the request was submitted while they were
    # hourly. The projection must follow the stamp, so a vacation reads as the
    # hourly "counted" case (no pot moves), matching what approval will do.
    staff = {"id": "7", "fields": {**EMP, "SalaryHourly": "Salary"}}
    sp_user_to_name = _PersonNames()
    sp_user_to_name.by_email["pat@ucsh.com"] = "Pat Staff"
    item_data = {
        "id": "300", "LeaveType": "Vacation", "Days": 2,
        "StartDate": "2026-11-02", "EndDate": "2026-11-03",
        "SubmitterEmail": "pat@ucsh.com", "RequestEmploymentType": "Hourly",
    }

    _enrich_pending_item(item_data, "leave", {"pat staff": staff}, {7: staff}, sp_user_to_name)

    assert item_data["projected_balances"] is None
    assert item_data["balance_unchanged"] == "hourly: counted as vacation taken, no balance used"


# ---------------------------------------------------------------------------
# The hourly vacation counter follows the stamped type too (finding 5)
# ---------------------------------------------------------------------------

def test_vacation_days_counted_skips_rows_stamped_non_hourly():
    # Only days approved while hourly are the hourly counter; a vacation approved
    # while salaried spent pots and must not be counted. A row without the stamp
    # (SharePoint-era) keeps the old behaviour and counts.
    rows = [
        {"Status": "Approved", "LeaveType": "Vacation", "StartDate": "2026-05-01",
         "Days": 2, "RequestEmploymentType": "Hourly"},            # hourly: counts
        {"Status": "Approved", "LeaveType": "Vacation", "StartDate": "2026-06-01",
         "Days": 3, "RequestEmploymentType": "Salary"},            # salaried then: skipped
        {"Status": "Approved", "LeaveType": "Vacation", "StartDate": "2026-07-01",
         "Days": 1},                                               # no stamp: legacy, counts
    ]
    assert vacation_days_counted(rows, 2026) == 3.0
