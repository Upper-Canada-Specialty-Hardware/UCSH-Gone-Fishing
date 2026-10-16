"""The SharePoint-to-Postgres request import, sync, switch and checks (#333-#339).

Everything runs on SQLite with a faked ``sp_client``: the Staff Directory, the
site user list and the three request lists are in-memory dicts, so no test
touches real SharePoint. The suite covers the importer (idempotency, item ids
and counters, the leave-title split, the legacy flag, the audit log to ledger,
and each request-side detector), the single-runner shadow sync, the flip's
final-sync-then-setting, the post-switch SharePoint check (new Form item, edit,
delete, and two look-alike items), and the rehearsal gate.
"""
import asyncio
import copy
import json

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.app_setting import AppSetting
from app.models.balance_ledger import BalanceLedger
from app.models.legacy_finding import LegacyFinding
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.models.sync_run import SyncRun
from app.services import app_settings, rehearsal, shadow_sync
from app.services.app_settings import POSTGRES, REQUESTS_STORAGE
from app.services.request_import import import_requests
from app.services.sharepoint_check import run_sharepoint_check
from app.services.storage_switch import flip_to_postgres

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
OVERTIME = settings.SP_LIST_OVERTIME_REQUESTS
CARRYOVER = settings.SP_LIST_CARRYOVER_PAYOUT
STAFF = settings.SP_LIST_STAFF_DIRECTORY
USERS = "User Information List"

# Staff Directory: a salaried, an hourly, and one whose site name is a username.
STAFF_ITEMS = [
    {"id": "10", "fields": {"Title": "Alice Salary", "EmailAddress": "alice@ucsh.com",
                            "SalaryHourly": "Salary", "Location": "Toronto Warden", "Department": "Sales"}},
    {"id": "20", "fields": {"Title": "Bob Hourly", "EmailAddress": "bob@ucsh.com",
                            "SalaryHourly": "Hourly", "Location": "British Columbia", "Department": "Warehouse"}},
    {"id": "30", "fields": {"Title": "Jane Doe", "EmailAddress": "jdoe@ucsh.com", "SalaryHourly": "Salary"}},
]
# Site User Information List: lookup id -> name and email. User 103's name is a
# login ("j.doe"), which the username_as_name detector should catch.
USER_ITEMS = [
    {"id": "101", "fields": {"Title": "Alice Salary", "EMail": "alice@ucsh.com"}},
    {"id": "102", "fields": {"Title": "Bob Hourly", "EMail": "bob@ucsh.com"}},
    {"id": "103", "fields": {"Title": "j.doe", "EMail": "jdoe@ucsh.com"}},
]

# Tables this suite owns and clears between tests.
_TABLES = [BalanceLedger, RequestEvent, RequestChange, LegacyFinding, SharepointSeen,
           RequestRecord, RequestCounter, SyncRun, AppSetting]


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


async def _wipe():
    """Empty every table this suite writes, so each test starts clean."""
    async with async_session() as session:
        for model in _TABLES:
            await session.execute(delete(model))
        await session.commit()


class FakeSharePoint:
    """An in-memory stand-in for sp_client: lists keyed by SharePoint list id."""

    def __init__(self):
        self.lists = {
            STAFF: [copy.deepcopy(i) for i in STAFF_ITEMS],
            USERS: [copy.deepcopy(i) for i in USER_ITEMS],
            LEAVE: [], OVERTIME: [], CARRYOVER: [],
        }

    async def get_list_items(self, list_id, filter=None, select=None, expand="fields", top=5000):
        """Every item of a list, copied so a caller cannot mutate the store."""
        return [copy.deepcopy(i) for i in self.lists.get(list_id, [])]

    async def get_list_item_or_none(self, list_id, item_id):
        """One item by id, or None."""
        for item in self.lists.get(list_id, []):
            if str(item["id"]) == str(item_id):
                return copy.deepcopy(item)
        return None


@pytest.fixture
def sp(monkeypatch):
    """A faked sp_client and a clean database, reset after the test."""
    fake = FakeSharePoint()
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")   # the switch is driven by the DB setting
    run(_wipe())
    yield fake
    run(_wipe())


# ----- item builders -----


def sp_item(item_id, fields, created="2026-05-01T10:00:00Z", modified="2026-05-01T10:00:00Z"):
    """A SharePoint list item in the Graph shape, eTag following the modified time."""
    return {"id": str(item_id), "createdDateTime": created, "lastModifiedDateTime": modified,
            "eTag": modified, "fields": dict(fields)}


def leave_fields(lookup=101, leave_type="Vacation", start="2026-06-01", end="2026-06-05",
                 days=5, status="Pending", title=None, approved=None, audit=None,
                 submitter_email=None, request_source=None, **extra):
    """SharePoint leave-request fields, person named by its site lookup id."""
    f = {"LeaveType": leave_type, "StartDate": start, "EndDate": end, "Days": days,
         "Status": status, "SubmittedTestLookupId": lookup}
    if title is not None:
        f["Title"] = title
    if approved is not None:
        f["ApprovedDate"] = approved
    if audit is not None:
        f["BalanceAuditLog"] = audit
    if submitter_email is not None:
        f["SubmitterEmail"] = submitter_email
    if request_source is not None:
        f["RequestSource"] = request_source
    f.update(extra)
    return f


def audit_log(before, after, action="approve", operation="cascade"):
    """A BalanceAuditLog JSON string of one entry with one before/after step."""
    return json.dumps([{"action": action, "timestamp": "2026-06-02T10:00:00",
                        "steps": [{"operation": operation, "before": before, "after": after}]}])


async def _record(kind, item_id):
    """The imported RequestRecord for a kind and item id, or None."""
    async with async_session() as session:
        return (await session.execute(
            select(RequestRecord).where(RequestRecord.kind == kind, RequestRecord.item_id == int(item_id))
        )).scalars().first()


async def _count(model, **where):
    """How many rows of a model match the given equality filters."""
    async with async_session() as session:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        return int((await session.execute(stmt)).scalar_one())


async def _findings(case):
    """Every legacy finding of one case."""
    async with async_session() as session:
        return (await session.execute(
            select(LegacyFinding).where(LegacyFinding.case == case)
        )).scalars().all()


# ----- importer -----


def test_import_is_idempotent(sp):
    # Two items; running the import twice must not double rows, events or ledger.
    sp.lists[LEAVE] = [sp_item(3905, leave_fields(
        status="Approved", approved="2026-06-02",
        audit=audit_log({"CurrentVacationBalance": 10}, {"CurrentVacationBalance": 5})))]
    sp.lists[OVERTIME] = [sp_item(7, {"Title": "Worked late", "Date": "2026-06-01",
                                      "Hours": 4, "Status": "Pending", "SubmittedByLookupId": 101})]
    run(import_requests())
    run(import_requests())
    assert run(_count(RequestRecord)) == 2                       # still one row per item
    assert run(_count(RequestEvent, type="imported")) == 2       # one imported event each
    assert run(_count(BalanceLedger)) == 1                       # the single vacation movement, once


def test_item_ids_kept_and_counters_raised(sp):
    # The SharePoint item id becomes the item number, and the counter sits above it.
    sp.lists[LEAVE] = [sp_item(3905, leave_fields())]
    run(import_requests())
    record = run(_record("leave", 3905))
    assert record is not None and record.item_id == 3905 and record.sp_item_id == "3905"
    async def counter():
        async with async_session() as session:
            return await session.get(RequestCounter, "leave")
    assert run(counter()).last_item_id >= 3905


def test_leave_title_splits_both_ways(sp):
    # App format "<name> /// <notes>" and Form format "<desc> /// <relation>".
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=101, title="Alice Salary /// Doctor visit")),      # app format
        sp_item(2, leave_fields(lookup=102, leave_type="Bereavement",
                                title="Grandmother passed /// Grandmother")),             # form format
    ]
    run(import_requests())
    app_row = run(_record("leave", 1))
    form_row = run(_record("leave", 2))
    assert app_row.description == "Doctor visit" and app_row.bereavement_relation is None
    assert form_row.description == "Grandmother passed" and form_row.bereavement_relation == "Grandmother"


def test_legacy_flag_and_unknown_state(sp):
    # Decided before the cutoff with no audit proof -> legacy, legacy_unknown.
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(status="Approved", approved="2026-01-15")),               # before cutoff
        sp_item(2, leave_fields(status="Approved", approved="2026-06-02")),               # after cutoff
    ]
    run(import_requests())
    legacy = run(_record("leave", 1))
    current = run(_record("leave", 2))
    assert legacy.legacy is True and legacy.balance_state == "legacy_unknown"
    assert current.legacy is False and current.balance_state == "not_applied"


def test_audit_log_becomes_ledger_rows(sp):
    # The approve entry's pot movements become ledger rows and set the state.
    sp.lists[LEAVE] = [sp_item(1, leave_fields(
        status="Approved", approved="2026-06-02",
        audit=audit_log({"CurrentVacationBalance": 10, "CurrentSickDayBalance": 3},
                        {"CurrentVacationBalance": 8, "CurrentSickDayBalance": 3})))]
    run(import_requests())
    record = run(_record("leave", 1))
    assert record.balance_state == "applied"
    async def ledger():
        async with async_session() as session:
            return (await session.execute(
                select(BalanceLedger).where(BalanceLedger.request_id == record.id)
            )).scalars().all()
    rows = run(ledger())
    assert len(rows) == 1                                        # only vacation moved; sick was unchanged
    assert rows[0].pot == "vacation" and rows[0].before == 10 and rows[0].after == 8
    assert rows[0].idempotency_key == "import:leave:1:ledger:0"


# ----- detectors (each fires on its case and stays quiet on a clean case) -----


def test_detector_approved_no_balance_change(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=101, status="Approved", approved="2026-06-02")),   # no audit -> fires
        sp_item(2, leave_fields(lookup=101, status="Approved", approved="2026-06-02",
                                audit=audit_log({"CurrentVacationBalance": 10},
                                                {"CurrentVacationBalance": 8}))),          # clean
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("approved_no_balance_change"))}
    assert fired == {"1"}


def test_detector_employment_type_suspect(sp):
    sp.lists[OVERTIME] = [
        sp_item(1, {"Title": "OT", "Date": "2026-06-01", "Hours": 4, "Status": "Approved",
                    "ApprovedDate": "2026-06-02", "SubmittedByLookupId": 102,              # Bob is hourly
                    "BalanceAuditLog": audit_log({"CurrentOvertimeBalance": 0},
                                                 {"CurrentOvertimeBalance": 5})})]          # salaried pot moved -> fires
    sp.lists[LEAVE] = [
        sp_item(2, leave_fields(lookup=102, leave_type="Sick or Personal Day", status="Approved",
                                approved="2026-06-02",
                                audit=audit_log({"CurrentSickDayBalance": 5},
                                                {"CurrentSickDayBalance": 4})))]            # clean hourly sick
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("employment_type_suspect"))}
    assert fired == {"1"}


def test_detector_hourly_vacation_leftover(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=102, leave_type="Vacation", status="Approved",
                                approved="2026-06-02",
                                audit=audit_log({"CurrentVacationBalance": 10},
                                                {"CurrentVacationBalance": 8}))),           # hourly vacation deducted -> fires
        sp_item(2, leave_fields(lookup=102, leave_type="Vacation", status="Approved",
                                approved="2026-06-02")),                                    # clean (counted, no pot)
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("hourly_vacation_leftover"))}
    assert fired == {"1"}


def test_detector_date_error(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=101, start="2026-06-10", end="2026-06-01")),        # end before start -> fires
        sp_item(2, leave_fields(lookup=101, start="2026-06-01", end="2026-06-05")),        # clean
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("date_error"))}
    assert fired == {"1"}
    # The row still imported, with the impossible end date cleared (not applied silently).
    assert run(_record("leave", 1)).end_date is None


def test_detector_stuck_cancelled(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=101, status="Cancelled", approved="2026-06-02",
                                audit=audit_log({"CurrentVacationBalance": 10},
                                                {"CurrentVacationBalance": 8}))),           # cancelled after a change -> fires
        sp_item(2, leave_fields(lookup=101, status="Cancelled")),                          # clean (no change)
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("stuck_cancelled"))}
    assert fired == {"1"}


def test_detector_refunded_marked_processed(sp):
    sp.lists[CARRYOVER] = [
        sp_item(1, {"TypeofRequest": "Payout", "Days": 5, "Status": "Refunded",
                    "SystemState": "Processed", "SubmittedByLookupId": 101}),              # still processed -> fires
        sp_item(2, {"TypeofRequest": "Payout", "Days": 5, "Status": "Refunded",
                    "SystemState": "Not Processed", "SubmittedByLookupId": 101}),          # clean
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("refunded_marked_processed"))}
    assert fired == {"1"}


def test_detector_submitter_unmatched(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=999)),                                              # no such user -> fires
        sp_item(2, leave_fields(lookup=101)),                                              # clean (Alice)
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("submitter_unmatched"))}
    assert fired == {"1"}


def test_detector_username_as_name(sp):
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=103)),                                              # site name "j.doe" -> fires
        sp_item(2, leave_fields(lookup=101)),                                              # clean (Alice Salary)
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("username_as_name"))}
    assert fired == {"1"}


# ----- shadow sync -----


def test_shadow_sync_is_single_runner(sp):
    # While another runner holds the lock, run_shadow_sync does nothing.
    async def scenario():
        await shadow_sync._single_process_lock.acquire()        # stand in for a second instance
        try:
            return await shadow_sync.run_shadow_sync()
        finally:
            shadow_sync._single_process_lock.release()
    assert run(scenario()).get("skipped") == "locked"


# ----- the flip -----


def test_flip_runs_a_final_sync_then_sets_the_storage(sp):
    sp.lists[LEAVE] = [sp_item(3905, leave_fields(lookup=101))]
    result = run(flip_to_postgres())
    assert result["storage"] == POSTGRES
    assert run(app_settings.get_setting(REQUESTS_STORAGE)) == POSTGRES   # the setting flipped
    assert run(_count(SyncRun, job="final_sync")) == 1                   # a final sync was recorded
    assert run(_record("leave", 3905)) is not None                      # the final sync imported the item


# ----- the post-switch SharePoint check -----


def _switch_to_postgres():
    """Put requests in Postgres for the post-switch checks."""
    run(app_settings.set_setting(REQUESTS_STORAGE, POSTGRES))


def test_check_imports_a_new_form_item_once_across_three_runs(sp):
    _switch_to_postgres()
    # A Form submission has a blank RequestSource.
    sp.lists[LEAVE] = [sp_item(500, leave_fields(lookup=101, request_source=""))]
    for _ in range(3):
        run(run_sharepoint_check())
    assert run(_count(RequestRecord, kind="leave", sp_item_id="500")) == 1
    assert run(_count(RequestEvent, type="submitted", channel="form")) == 1


def test_check_flags_an_edit_once_and_never_applies_it(sp):
    # Import the item, switch, then edit it in SharePoint.
    sp.lists[LEAVE] = [sp_item(600, leave_fields(lookup=101, days=5), modified="2026-05-01T10:00:00Z")]
    run(import_requests())
    _switch_to_postgres()
    run(run_sharepoint_check())                                 # baseline sighting, no flag
    sp.lists[LEAVE] = [sp_item(600, leave_fields(lookup=101, days=9), modified="2026-07-01T10:00:00Z")]
    run(run_sharepoint_check())                                 # a new version -> edit flagged
    run(run_sharepoint_check())                                 # same new version -> no re-flag
    assert len(run(_findings("sharepoint_edit_after_switch"))) == 1
    assert run(_record("leave", 600)).days == 5                 # the edit was not applied


def test_an_edit_right_after_the_switch_is_still_flagged(sp):
    """An edit between the final sync and the first check is not taken as the baseline."""
    sp.lists[LEAVE] = [sp_item(650, leave_fields(lookup=101, days=5), modified="2026-05-01T10:00:00Z")]
    run(flip_to_postgres())                                     # the final sync records this version
    sp.lists[LEAVE] = [sp_item(650, leave_fields(lookup=101, days=9), modified="2026-05-01T10:05:00Z")]
    run(run_sharepoint_check())                                 # the very first check sees the edit
    assert len(run(_findings("sharepoint_edit_after_switch"))) == 1
    assert run(_record("leave", 650)).days == 5                 # and never applies it


def test_check_flags_a_delete_once_and_never_applies_it(sp):
    sp.lists[LEAVE] = [sp_item(700, leave_fields(lookup=101))]
    run(import_requests())
    _switch_to_postgres()
    run(run_sharepoint_check())                                 # baseline
    sp.lists[LEAVE] = []                                        # the item is gone from SharePoint
    run(run_sharepoint_check())                                 # delete flagged
    run(run_sharepoint_check())                                 # no re-flag
    assert len(run(_findings("sharepoint_delete_after_switch"))) == 1
    assert run(_record("leave", 700)) is not None               # the Postgres row is kept


def test_check_keeps_two_look_alike_items_apart(sp):
    _switch_to_postgres()
    # Same text, different SharePoint ids: matched by id, so both are kept.
    sp.lists[LEAVE] = [
        sp_item(800, leave_fields(lookup=101, title="Same text", request_source="")),
        sp_item(801, leave_fields(lookup=101, title="Same text", request_source="")),
    ]
    run(run_sharepoint_check())
    assert run(_count(RequestRecord, kind="leave")) == 2


# ----- rehearsal -----


def test_rehearsal_blocks_webhook_registration(monkeypatch):
    from app.tasks import subscription_manager
    calls = []

    async def spy(list_id):
        calls.append(list_id)

    monkeypatch.setattr(subscription_manager, "_ensure_subscription", spy)
    monkeypatch.setattr(settings, "REHEARSAL_MODE", True)
    run(subscription_manager.register_all_subscriptions())
    assert calls == []                                          # no subscription was registered


def test_rehearsal_blocks_sharepoint_writes(monkeypatch):
    monkeypatch.setattr(settings, "REHEARSAL_MODE", True)

    async def boom(*args, **kwargs):
        raise AssertionError("a SharePoint write was attempted in rehearsal")

    monkeypatch.setattr(sharepoint.graph_client, "post", boom)
    monkeypatch.setattr(sharepoint.graph_client, "patch", boom)
    monkeypatch.setattr(sharepoint.graph_client, "delete", boom)
    assert run(sharepoint.sp_client.create_list_item(LEAVE, {"Title": "x"})) == {}
    assert run(sharepoint.sp_client.update_list_item_fields(LEAVE, "1", {"Title": "x"})) == {}
    assert run(sharepoint.sp_client.delete_list_item(LEAVE, "1")) is None


def test_rehearsal_drops_non_allowlisted_sends(monkeypatch):
    from app.services import sms
    monkeypatch.setattr(settings, "REHEARSAL_MODE", True)
    monkeypatch.setattr(settings, "REHEARSAL_ALLOWLIST", "ok@ucsh.com, +16475550100")
    assert rehearsal.allow_recipient("ok@ucsh.com") is True
    assert rehearsal.allow_recipient("(647) 555-0100") is True   # same number, different formatting
    assert rehearsal.allow_recipient("stranger@example.com") is False
    assert run(sms.send_sms("+16475559999", "hi")) == {"skipped": "rehearsal"}
