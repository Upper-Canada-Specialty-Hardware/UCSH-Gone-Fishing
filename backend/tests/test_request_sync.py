"""The SharePoint-to-Postgres request import, sync, switch and checks (#333-#339).

Everything runs on SQLite with a faked ``sp_client``: the Staff Directory, the
site user list and the three request lists are in-memory dicts, so no test
touches real SharePoint. The suite covers the importer (idempotency, item ids
and counters, the leave-title split, the legacy flag, the audit log to ledger,
and each request-side detector), the single-runner shadow sync, the flip's
final-sync-then-setting (and its refusal to run twice), the post-switch SharePoint
check (a new item flagged not imported, edit, delete, and two look-alike items),
the switch pause, the imported approval version, and the rehearsal gate.
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
from app.models.request_approval_state import RequestApprovalState
from app.models.request_change import RequestChange
from app.models.request_counter import RequestCounter
from app.models.request_deletion import RequestDeletion
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.models.sharepoint_seen import SharepointSeen
from app.models.sync_run import SyncRun
from app.services import app_settings, rehearsal, shadow_sync
from app.services.app_settings import HOLIDAYS_STORAGE, POSTGRES, REQUESTS_STORAGE, STAFF_STORAGE
from app.services.request_import import import_requests
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
           RequestRecord, RequestCounter, RequestApprovalState, RequestDeletion, SyncRun, AppSetting]


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


@pytest.fixture(autouse=True)
def _no_settle_wait(monkeypatch):
    """Skip the switch's post-pause wait so these tests do not sleep."""
    monkeypatch.setattr("app.services.storage_switch.PAUSE_SETTLE_SECONDS", 0)


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


def test_the_import_stamps_the_sharepoint_version_and_keeps_what_the_history_found(sp):
    # The history step (#357) reads an item again only when its version moves,
    # and a re-import must not undo the balance state it set.
    sp.lists[LEAVE] = [sp_item(1, leave_fields(status="Approved", approved="2026-06-02", _UIVersionString="3.0"))]
    run(import_requests())
    record = run(_record("leave", 1))
    assert record.extra["sp_version"] == "3.0" and record.balance_state == "not_applied"

    async def found_by_history():
        async with async_session() as session:
            row = (await session.execute(select(RequestRecord).where(RequestRecord.id == record.id))).scalar_one()
            row.extra = {**row.extra, "sp_history": {"version": "3.0", "balance": "matched"}}
            row.balance_state = "applied"
            await session.commit()
    run(found_by_history())
    sp.lists[LEAVE] = [sp_item(1, leave_fields(status="Approved", approved="2026-06-02", _UIVersionString="4.0"),
                               modified="2026-06-03T10:00:00Z")]
    run(import_requests())
    record = run(_record("leave", 1))
    assert record.extra["sp_version"] == "4.0" and record.balance_state == "applied"


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


def test_old_carryovers_without_a_log_are_not_flagged(sp):
    # Before the app approved anything (2026-03-23), carry-overs and payouts were
    # moved by hand and wrote no balance log, so they are not findings. One
    # approved since then with no log is still flagged.
    sp.lists[CARRYOVER] = [
        sp_item(1, {"TypeofRequest": "Carry Over", "Days": 3, "Status": "Approved",
                    "ApprovedDate": "2024-12-10", "SubmittedByLookupId": 101}),            # old, no log -> quiet
        sp_item(2, {"TypeofRequest": "Payout", "Days": 2, "Status": "Approved",
                    "SubmittedByLookupId": 101}, created="2023-11-01T10:00:00Z"),          # old, no date -> quiet
        sp_item(3, {"TypeofRequest": "Carry Over", "Days": 3, "Status": "Approved",
                    "ApprovedDate": "2026-06-02", "SubmittedByLookupId": 101}),            # since the app -> fires
    ]
    run(import_requests())
    fired = {f.subject_id for f in run(_findings("approved_no_balance_change"))}
    assert fired == {"3"}


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


# ----- after the switch -----


def _switch_to_postgres():
    """Put requests, staff and holidays in Postgres for the post-switch checks.

    All three settings are flipped, so the switch reads as fully done (the flip
    refuses a second run only when every backend is already Postgres).
    """
    run(app_settings.set_setting(REQUESTS_STORAGE, POSTGRES))
    run(app_settings.set_setting(STAFF_STORAGE, POSTGRES))
    run(app_settings.set_setting(HOLIDAYS_STORAGE, POSTGRES))


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


def test_rehearsal_never_adds_columns_to_the_live_lists(monkeypatch):
    # The admin "add request columns" button changes the live SharePoint lists,
    # so in rehearsal it reports only and never posts a column.
    from app.services import request_submitter
    monkeypatch.setattr(settings, "REHEARSAL_MODE", True)
    monkeypatch.setattr(sharepoint.sp_client, "site_id", "site")

    async def no_columns(path, params=None):
        return {"value": []}                                    # every list is missing both columns

    async def boom(*args, **kwargs):
        raise AssertionError("a SharePoint column was added in rehearsal")

    monkeypatch.setattr(request_submitter.graph_client, "get", no_columns)
    monkeypatch.setattr(request_submitter.graph_client, "post", boom)
    report = run(request_submitter.ensure_request_columns(create=True))
    assert all(entry["added"] == [] for entry in report["lists"].values())
    assert all(len(entry["missing"]) == 2 for entry in report["lists"].values())


def test_rehearsal_drops_non_allowlisted_sends(monkeypatch):
    from app.services import sms
    monkeypatch.setattr(settings, "REHEARSAL_MODE", True)
    monkeypatch.setattr(settings, "REHEARSAL_ALLOWLIST", "ok@ucsh.com, +16475550100")
    assert rehearsal.allow_recipient("ok@ucsh.com") is True
    assert rehearsal.allow_recipient("(647) 555-0100") is True   # same number, different formatting
    assert rehearsal.allow_recipient("stranger@example.com") is False
    assert run(sms.send_sms("+16475559999", "hi")) == {"skipped": "rehearsal"}


# ----- the flip refuses a second run -----


def test_flip_is_refused_after_the_switch(sp):
    # A second flip would overwrite Postgres decisions with SharePoint's values.
    from app.services.storage_switch import AlreadySwitched
    _switch_to_postgres()
    with pytest.raises(AlreadySwitched):
        run(flip_to_postgres())


# ----- a failed item is recorded and retried -----


def test_a_failed_item_is_recorded_and_retried(sp, monkeypatch):
    # One item raises on import; it must be visible in the counts, left unimported
    # and unseen (so the next run retries it), while the good item still lands.
    from app.services import request_import as ri
    sp.lists[LEAVE] = [sp_item(10, leave_fields(lookup=101)), sp_item(66, leave_fields(lookup=101))]
    real = ri._import_item

    async def flaky(session, kind, sp_item, *args, **kwargs):
        if str(sp_item.get("id")) == "66":
            raise RuntimeError("boom")                            # simulate a bad item
        return await real(session, kind, sp_item, *args, **kwargs)

    monkeypatch.setattr(ri, "_import_item", flaky)
    counts = run(import_requests())
    assert counts["leave"]["errors"] == 1 and "66" in counts["leave"]["error_ids"]   # recorded and visible
    assert run(_record("leave", 66)) is None                     # not imported
    assert run(_record("leave", 10)) is not None                 # the good one still landed

    monkeypatch.setattr(ri, "_import_item", real)                # the item stops failing
    run(import_requests())
    assert run(_record("leave", 66)) is not None                 # retried and imported


# ----- the switch pause refuses writes, and expires on its own -----


def test_a_decision_during_the_pause_is_refused(sp):
    # While the pause is on, an approval is refused with the friendly message.
    from app.services.leave_requests import approve_leave_request
    from app.services.switch_pause import PAUSE_MESSAGE, SystemPaused, begin_pause
    run(begin_pause())
    with pytest.raises(SystemPaused) as caught:
        run(approve_leave_request("1", "10", "manager_dashboard"))
    assert caught.value.message == PAUSE_MESSAGE


def test_the_pause_expires(sp):
    # A pause past its safety expiry reads as cleared, so a crash mid-flip never
    # leaves the app paused forever.
    from datetime import datetime, timedelta, timezone
    from app.services import switch_pause
    run(switch_pause.begin_pause())
    assert run(switch_pause.is_paused()) is True
    later = datetime.now(timezone.utc) + switch_pause.PAUSE_MAX + timedelta(seconds=1)
    assert run(switch_pause.is_paused(later)) is False


# ----- the importer carries the approval version, and a v2 link approves -----


class _FakeStaffBalances:
    """An in-memory StaffBalances for the decision path (no real backend)."""

    def __init__(self):
        """Start every stored pot at zero."""
        from app.repositories.staff_balances import POT_COLUMNS
        self._pots = {pot: 0.0 for pot in POT_COLUMNS}           # the five spendable pots

    async def lock_pots(self, session, employee_id):
        """Return a copy of the current pots."""
        return dict(self._pots)

    async def write_pots(self, session, employee_id, new_values):
        """Apply a pot write."""
        self._pots.update(new_values)


def test_import_carries_the_approval_version_and_a_v2_link_approves(sp, monkeypatch):
    from app.models.request_approval_state import RequestApprovalState
    from app.services import request_decisions as rd

    # A leave request edited before the switch: its approval state is at version 2,
    # and its latest emailed link carries v2.
    async def seed_state():
        async with async_session() as session:
            session.add(RequestApprovalState(
                list_id=LEAVE, item_id="3905", current_version=2, current_snapshot={}))
            await session.commit()

    run(seed_state())
    # A manager on the row so the import maps it to waiting_on_approval (a pending
    # row with a manager and an imported event), the only decidable state (#455).
    sp.lists[LEAVE] = [sp_item(3905, leave_fields(lookup=101, ManagerID="55"))]
    run(import_requests())
    assert run(_record("leave", 3905)).approval_version == 2     # carried from the approval state
    assert run(_record("leave", 3905)).state == "waiting_on_approval"   # ready to be decided (#455)

    fake = _FakeStaffBalances()

    async def _get():
        return fake

    monkeypatch.setattr(rd, "get_staff_balances", _get)          # the decision path reads this
    stale = run(rd.approve("leave", 3905, "Alice Salary", "email_link", link_version=1))
    assert stale.get("refused") == "stale"                       # an old v1 link is now stale
    ok = run(rd.approve("leave", 3905, "Alice Salary", "email_link", link_version=2))
    assert ok.get("status") == "approved"                        # the live v2 link approves


# ----- the fixes before the rehearsal (#335, #336, #341) -----


def test_a_failed_user_list_read_fails_the_import(sp, monkeypatch):
    # A failed User Information List read must fail the whole run, not be swallowed,
    # so a run that cannot resolve submitters never downgrades matched rows (item 2).
    sp.lists[LEAVE] = [sp_item(1, leave_fields(lookup=101))]
    real = sp.get_list_items

    async def flaky(list_id, **kwargs):
        if list_id == USERS:
            raise RuntimeError("user list unavailable")
        return await real(list_id, **kwargs)

    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", flaky)
    with pytest.raises(RuntimeError):
        run(import_requests())


def test_a_prior_match_is_not_downgraded_on_a_later_non_match(sp):
    # Alice matched by email on the first import; when she later drops out of the
    # directory the row must keep her id, not fall back and file a finding (item 2).
    sp.lists[LEAVE] = [sp_item(50, leave_fields(lookup=999, submitter_email="alice@ucsh.com"))]
    run(import_requests())
    assert run(_record("leave", 50)).employee_id == "10"         # Alice's staff id

    sp.lists[STAFF] = [i for i in sp.lists[STAFF] if i["id"] != "10"]   # Alice is gone
    run(import_requests())
    record = run(_record("leave", 50))
    assert record.employee_id == "10"                            # kept, not downgraded to a fallback
    assert run(_findings("submitter_unmatched")) == []           # and not flagged as unmatched


def test_counter_lifts_above_approval_state_and_deletions(sp):
    # The counter must clear every number a SharePoint item ever held: imported
    # requests, approval-version rows, and the deletion log (item 5).
    async def seed():
        async with async_session() as session:
            session.add(RequestApprovalState(
                list_id=LEAVE, item_id="5000", current_version=1, current_snapshot={}))
            session.add(RequestDeletion(
                request_type="leave", list_item_id="6000", fields={}, reason="removed"))
            await session.commit()

    run(seed())
    sp.lists[LEAVE] = [sp_item(100, leave_fields(lookup=101))]
    run(import_requests())

    async def counter():
        async with async_session() as session:
            return await session.get(RequestCounter, "leave")

    assert run(counter()).last_item_id >= 6000                   # above the deleted number, not just 100


def test_staff_index_first_wins_on_a_duplicate_email(sp):
    # Two records share an email; the request index must match the FIRST, as the
    # staff import's first-wins rule does (item 6).
    sp.lists[STAFF] = [
        {"id": "40", "fields": {"Title": "First Dup", "EmailAddress": "dup@ucsh.com", "SalaryHourly": "Salary"}},
        {"id": "41", "fields": {"Title": "Second Dup", "EmailAddress": "dup@ucsh.com", "SalaryHourly": "Salary"}},
    ]
    sp.lists[LEAVE] = [sp_item(70, leave_fields(lookup=999, submitter_email="dup@ucsh.com"))]
    run(import_requests())
    assert run(_record("leave", 70)).employee_id == "40"         # the first record wins


def test_staff_index_shared_email_goes_to_the_lowest_id_whatever_the_read_order(sp):
    # SharePoint lists staff in id order and SharePoint mode took the first match,
    # so a shared email resolves to the lowest id even when a read returns them reversed.
    sp.lists[STAFF] = [
        {"id": "41", "fields": {"Title": "Second Dup", "EmailAddress": "dup@ucsh.com", "SalaryHourly": "Salary"}},
        {"id": "40", "fields": {"Title": "First Dup", "EmailAddress": "dup@ucsh.com", "SalaryHourly": "Salary"}},
    ]
    sp.lists[LEAVE] = [sp_item(71, leave_fields(lookup=999, submitter_email="dup@ucsh.com"))]
    run(import_requests())
    assert run(_record("leave", 71)).employee_id == "40"


def test_staff_index_matches_the_site_user_by_name_as_sharepoint_mode_did(sp):
    # No email on the request: the site user's display name picks the staff record,
    # as resolve_person_field did, even when that user's site email is someone else's.
    sp.lists[USERS].append({"id": "104", "fields": {"Title": "Bob Hourly", "EMail": "alice@ucsh.com"}})
    sp.lists[LEAVE] = [sp_item(72, leave_fields(lookup=104))]
    run(import_requests())
    assert run(_record("leave", 72)).employee_id == "20"         # Bob by name, not Alice by email


def test_staff_index_falls_back_to_the_site_users_email_when_nothing_else_matches(sp):
    # The site user's name is a login no staff record carries: the email names them,
    # so a row SharePoint mode could not place still gets an owner, not a fallback id.
    sp.lists[LEAVE] = [sp_item(73, leave_fields(lookup=103))]  # "j.doe", jdoe@ucsh.com
    run(import_requests())
    assert run(_record("leave", 73)).employee_id == "30"


def test_staff_index_shared_email_keeps_sharepoint_modes_pick_over_the_name(sp):
    # A shared email beats the person column, as it did in SharePoint mode: the
    # balance that request moved was the first record's, so the row stays with it.
    sp.lists[STAFF] = [
        {"id": "40", "fields": {"Title": "Accounts Desk", "EmailAddress": "ap@ucsh.com", "SalaryHourly": "Salary"}},
        {"id": "41", "fields": {"Title": "Real Person", "EmailAddress": "ap@ucsh.com", "SalaryHourly": "Salary"}},
    ]
    sp.lists[USERS].append({"id": "105", "fields": {"Title": "Real Person", "EMail": "ap@ucsh.com"}})
    sp.lists[LEAVE] = [sp_item(74, leave_fields(lookup=105, submitter_email="ap@ucsh.com"))]
    run(import_requests())
    assert run(_record("leave", 74)).employee_id == "40"


def test_detector_pending_unprocessed(sp):
    # A pending row the old pipeline never finished (no days or no manager) is
    # flagged; one with both is quiet (item 7).
    sp.lists[LEAVE] = [
        sp_item(1, {"LeaveType": "Vacation", "StartDate": "2026-06-01", "Status": "Pending",
                    "SubmittedTestLookupId": 101}),                               # no days, no manager -> fires
        sp_item(2, leave_fields(lookup=101, days=5, Managertxt="Boss")),          # has both -> quiet
    ]
    run(import_requests())
    assert {f.subject_id for f in run(_findings("pending_unprocessed"))} == {"1"}


def test_unknown_status_is_stored_as_cancelled_and_flagged(sp):
    # A Status outside the five known values is stored as cancelled with the raw
    # value kept, and flagged; a known value keeps its mapping (item 10).
    sp.lists[LEAVE] = [
        sp_item(1, leave_fields(lookup=101, status="In Review")),                 # unknown -> cancelled + finding
        sp_item(2, leave_fields(lookup=101, status="Approved", approved="2026-06-02")),  # known -> unchanged
    ]
    run(import_requests())
    unknown = run(_record("leave", 1))
    assert unknown.status == "cancelled"
    assert unknown.extra.get("Status_raw") == "In Review"
    assert {f.subject_id for f in run(_findings("unknown_status"))} == {"1"}
    assert run(_record("leave", 2)).status == "approved"         # the five keep their mapping


def test_absent_fields_are_cleared_on_reimport(sp):
    # Graph omits a cleared field, so a re-import must reset the mapped columns it
    # no longer carries: a shortened range, a removed day count and manager (item 11).
    sp.lists[LEAVE] = [sp_item(300, {"LeaveType": "Vacation", "StartDate": "2026-06-01",
                                     "EndDate": "2026-06-05", "Days": 5, "Status": "Pending",
                                     "SubmittedTestLookupId": 101, "Managertxt": "Boss", "ManagerID": "77"})]
    run(import_requests())
    first = run(_record("leave", 300))
    assert first.end_date is not None and first.days == 5 and first.manager_name == "Boss"

    sp.lists[LEAVE] = [sp_item(300, {"LeaveType": "Vacation", "StartDate": "2026-06-01",
                                     "Status": "Pending", "SubmittedTestLookupId": 101})]
    run(import_requests())
    second = run(_record("leave", 300))
    assert second.end_date is None and second.days is None and second.manager_name is None


def test_an_employee_id_whose_record_is_gone_is_kept_as_whole_number_text(sp):
    # SharePoint sends EmployeeID as a float; when that staff record no longer
    # exists the row keeps the id it names, as "458", never "458.0", on every run.
    sp.lists[CARRYOVER] = [sp_item(80, {"TypeofRequest": "Payout", "Days": 1, "Status": "Approved",
                                        "EmployeeID": 458.0})]
    run(import_requests())
    assert run(_record("carryover_payout", 80)).employee_id == "458"
    run(import_requests())                                       # a rerun leaves it the same
    assert run(_record("carryover_payout", 80)).employee_id == "458"
