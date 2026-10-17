"""The Staff Directory in Postgres behaves like the SharePoint list (#261, #265, #341).

These run on the local SQLite fallback (the same SQLAlchemy layer Postgres uses
under conftest), with ``sp_client`` faked so nothing touches a real list. They
cover: the store answers in the same shape from either backend; the import is
idempotent and builds the supervisor links; matching is by id then email and
never by name; the balance lock/write live inside the caller's transaction and a
rollback leaves the pots untouched; the write-back retries until it lands, claims
a row so a second drain never replays it, and reclaims a crashed claim; a pulled
edit is never echoed; the newer non-balance edit wins while the app-owned balances
are never overwritten (a differing SharePoint balance is filed as a finding), a
person with an unsettled write-back is left alone, a duplicate email imports
blanked; and each #341 detector fires on its case and stays quiet on a clean one.
"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint as sharepoint_module
from app.models.legacy_finding import LegacyFinding
from app.models.mixins import utcnow
from app.models.staff import (
    Department,
    Location,
    Staff,
    StaffSupervisor,
    StaffWriteback,
)
from app.models.staff_change import StaffChange
from app.repositories.postgres.staff_balances import PostgresStaffBalances
from app.repositories.staff_store import staff_store
from app.services import staff_import, staff_pull, staff_writeback
from app.services.person_match import match_person

STAFF = settings.SP_LIST_STAFF_DIRECTORY


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _sp_item(item_id, **overrides):
    """A sample SharePoint Staff Directory item, with field overrides.

    Args:
        item_id: The SharePoint item id (string).
        **overrides: Field values to set or replace on the base record.

    Returns:
        A ``{"id", "fields"}`` dict in Graph's shape.
    """
    fields = {
        "Title": "Ada Lovelace",
        "EmailAddress": "ada@ucsh.com",
        "SalaryHourly": "Salary",
        "Location": "Barrie",
        "Department": "Engineering",
        "Supervisor": "Chief",
        "DefaultYearlyVacationDays": 15,
        "SickDayEntitlement": 8,
        "CurrentVacationBalance": 10.5,
        "CurrentSickDayBalance": 3,
        "CurrentOvertimeBalance": 1.25,
        "CarryOver": 2,
        "Payout": 0,
        "CellNumber": "555-1212",
        "Extension": 204,
        "Birthday": "1990-12-10",
        "SystemCheck": "Ready",
        "RequestAllowDate": "2026-12-31",
        "Comments": "note",
        "TitleLinkLookupId": 55,
        "Modified": "2026-10-10T12:00:00Z",
        "Created": "2020-01-01T00:00:00Z",
        "AllManagers": [{"LookupId": 77, "LookupValue": "Grace Hopper"}],
        "SupervisorLinkLookupId": 77,
    }
    fields.update(overrides)
    return {"id": str(item_id), "fields": fields}


@pytest.fixture
def clean_db():
    """Empty every staff table before and after a test."""
    async def wipe():
        async with async_session() as session:
            for model in (StaffWriteback, StaffSupervisor, StaffChange, LegacyFinding, Staff,
                          Location, Department):
                await session.execute(delete(model))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


def _fake_list(monkeypatch, items):
    """Make sp_client.get_list_items return a fixed list for any list id."""
    async def fake_get_list_items(list_id, **kwargs):
        return list(items)
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_get_list_items)


def _import(items):
    """Run the staff import once over the given SharePoint items and commit."""
    async def inner():
        async with async_session() as session:
            counts = await staff_import.staff_sync_step(session, None)
            await session.commit()
            return counts
    return run(inner())


# --- Response shape: both backends agree --------------------------------------

def test_store_shape_matches_between_backends(clean_db, monkeypatch):
    item = _sp_item("101")
    _fake_list(monkeypatch, [item])
    _import([item])

    # Postgres backend through the store.
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    from_pg = run(staff_store.get_list_item(STAFF, "101"))

    # SharePoint backend through the store (faked sp_client returns the raw item).
    async def fake_get_item(list_id, item_id):
        return item
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_item", fake_get_item)
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    from_sp = run(staff_store.get_list_item(STAFF, "101"))

    assert from_pg["id"] == from_sp["id"] == "101"
    # The columns every caller reads must match across the two backends.
    for column in (
        "Title", "EmailAddress", "SalaryHourly", "Location", "Department", "Supervisor",
        "DefaultYearlyVacationDays", "SickDayEntitlement", "CurrentVacationBalance",
        "CurrentSickDayBalance", "CurrentOvertimeBalance", "CarryOver", "Payout",
        "CellNumber", "Extension", "Birthday", "SystemCheck", "RequestAllowDate",
        "Comments", "TitleLinkLookupId", "Modified", "AllManagers", "SupervisorLinkLookupId",
    ):
        assert from_pg["fields"][column] == from_sp["fields"][column], column


def test_a_blank_title_reads_as_empty_string_not_none(clean_db, monkeypatch):
    # Callers do fields["Title"].strip(), so a missing name must be "" not None.
    item = _sp_item("102", Title="")
    _fake_list(monkeypatch, [item])
    _import([item])
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    fields = run(staff_store.get_list_item(STAFF, "102"))["fields"]
    assert fields["Title"] == ""
    assert fields["EmailAddress"] == "ada@ucsh.com"


# --- Import: idempotent, with the supervisor links ----------------------------

def test_import_is_idempotent(clean_db, monkeypatch):
    item = _sp_item("101")
    _fake_list(monkeypatch, [item])
    first = _import([item])
    second = _import([item])

    assert first["imported"] == 1 and first["updated"] == 0
    assert second["imported"] == 0 and second["updated"] == 1   # same row, not a duplicate

    async def count_staff():
        async with async_session() as session:
            return (await session.execute(select(func.count()).select_from(Staff))).scalar_one()
    assert run(count_staff()) == 1


def test_import_builds_supervisor_links_and_relinks_by_name(clean_db, monkeypatch):
    # Ada is managed by Grace; importing both lets the manager link resolve to
    # Grace's own staff row (managers are links between staff rows).
    ada = _sp_item("101")
    grace = _sp_item("77", Title="Grace Hopper", EmailAddress="grace@ucsh.com",
                     AllManagers=[], SupervisorLinkLookupId=None)
    _fake_list(monkeypatch, [ada, grace])
    _import([ada, grace])

    async def read_links():
        async with async_session() as session:
            ada_row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            grace_row = (await session.execute(select(Staff).where(Staff.sp_item_id == "77"))).scalars().first()
            links = (await session.execute(
                select(StaffSupervisor).where(StaffSupervisor.staff_id == ada_row.id)
            )).scalars().all()
            return links, grace_row.id
    links, grace_id = run(read_links())

    relations = sorted(link.relation for link in links)
    assert relations == ["manager", "supervisor_link"]          # both person columns kept
    manager_link = next(link for link in links if link.relation == "manager")
    assert manager_link.manager_sp_user_id == 77
    assert manager_link.manager_name == "Grace Hopper"
    assert manager_link.manager_staff_id == grace_id            # relinked to Grace's staff row


# --- Matching: id then email, never name (#265) -------------------------------

def test_match_by_id_then_email_never_name(clean_db, monkeypatch):
    _fake_list(monkeypatch, [_sp_item("101")])
    _import([_sp_item("101")])

    assert run(match_person(staff_id="101"))["id"] == "101"                 # by id
    assert run(match_person(email="ADA@ucsh.com "))["id"] == "101"          # case + trailing space
    assert run(match_person(email="a d a @ucsh.com"))["id"] == "101"        # spaces inside
    assert run(match_person(email="Ada Lovelace")) is None                 # a name is not an email
    assert run(match_person()) is None                                     # nothing to match on


def test_match_id_wins_over_email(clean_db, monkeypatch):
    items = [_sp_item("101"), _sp_item("202", Title="Bob", EmailAddress="bob@ucsh.com")]
    _fake_list(monkeypatch, items)
    _import(items)
    # Ada's id with Bob's email: the id wins.
    matched = run(match_person(staff_id="101", email="bob@ucsh.com"))
    assert matched["id"] == "101"


# --- Balances: lock and write inside the caller's transaction -----------------

def _seed_one(monkeypatch, item):
    _fake_list(monkeypatch, [item])
    _import([item])


def test_lock_and_write_pots_commit(clean_db, monkeypatch):
    _seed_one(monkeypatch, _sp_item("101"))
    balances = PostgresStaffBalances()

    async def do():
        async with async_session() as session:
            pots = await balances.lock_pots(session, "101")
            assert pots["vacation"] == 10.5 and pots["sick"] == 3.0
            await balances.write_pots(session, "101", {"vacation": 8.0, "sick": 2.0})
            await session.commit()
        async with async_session() as session:                 # a fresh read sees the move
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            wb = (await session.execute(select(StaffWriteback))).scalars().all()
            return float(row.vacation_balance), float(row.sick_balance), wb
    vacation, sick, wb = run(do())
    assert (vacation, sick) == (8.0, 2.0)
    assert len(wb) == 1                                         # the move queued a write-back
    assert wb[0].fields["CurrentVacationBalance"] == 8.0


def test_rollback_leaves_the_pots_unchanged(clean_db, monkeypatch):
    _seed_one(monkeypatch, _sp_item("101"))
    balances = PostgresStaffBalances()

    async def do():
        async with async_session() as session:
            await balances.lock_pots(session, "101")
            await balances.write_pots(session, "101", {"vacation": 0.0, "sick": 0.0})
            await session.rollback()                            # the caller's transaction is undone
        async with async_session() as session:
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            wb = (await session.execute(select(StaffWriteback))).scalars().all()
            return float(row.vacation_balance), float(row.sick_balance), wb
    vacation, sick, wb = run(do())
    assert (vacation, sick) == (10.5, 3.0)                      # untouched
    assert wb == []                                            # the queued write-back rolled back too


# --- Write-back: retries until it lands ---------------------------------------

def test_writeback_retries_until_it_lands(clean_db, monkeypatch):
    _seed_one(monkeypatch, _sp_item("101"))
    monkeypatch.setattr(staff_writeback, "_backoff_seconds", lambda attempts: 0)  # retry at once

    async def enqueue():
        async with async_session() as session:
            staff_writeback.enqueue_writeback(session, "101", {"CurrentVacationBalance": 1.0})
            await session.commit()
    run(enqueue())

    attempts = {"n": 0}

    async def flaky_patch(list_id, item_id, fields):
        attempts["n"] += 1
        if attempts["n"] < 3:                                  # fail the first two tries
            raise RuntimeError("SharePoint is down")
        return {}
    monkeypatch.setattr(staff_writeback.sp_client, "update_list_item_fields", flaky_patch)

    run(staff_writeback.drain_writebacks())                    # fails
    run(staff_writeback.drain_writebacks())                    # fails
    run(staff_writeback.drain_writebacks())                    # lands

    async def status():
        async with async_session() as session:
            row = (await session.execute(select(StaffWriteback))).scalars().first()
            return row.status, row.sent_at
    final_status, sent_at = run(status())
    assert attempts["n"] == 3
    assert final_status == "done" and sent_at is not None


def test_writeback_skipped_in_rehearsal(clean_db, monkeypatch):
    _seed_one(monkeypatch, _sp_item("101"))

    async def enqueue():
        async with async_session() as session:
            staff_writeback.enqueue_writeback(session, "101", {"CurrentVacationBalance": 1.0})
            await session.commit()
    run(enqueue())
    monkeypatch.setattr(staff_writeback, "_writes_allowed", lambda: False)

    result = run(staff_writeback.drain_writebacks())
    assert result["skipped"] == -1 and result["sent"] == 0

    async def still_pending():
        async with async_session() as session:
            row = (await session.execute(select(StaffWriteback))).scalars().first()
            return row.status
    assert run(still_pending()) == "pending"                   # left for later


def test_a_claimed_writeback_is_not_replayed_by_a_second_drain(clean_db, monkeypatch):
    # A row another instance has claimed ("sending", claim not yet expired) is not
    # due, so a second drain never calls SharePoint for it.
    _seed_one(monkeypatch, _sp_item("101"))

    async def claim_elsewhere():
        async with async_session() as session:
            staff_writeback.enqueue_writeback(session, "101", {"CurrentVacationBalance": 1.0})
            await session.commit()
        async with async_session() as session:                 # stand in for the other instance's claim
            row = (await session.execute(select(StaffWriteback))).scalars().first()
            row.status = staff_writeback.SENDING               # in flight elsewhere
            row.next_attempt_at = utcnow() + timedelta(minutes=5)   # claim not yet expired
            await session.commit()
    run(claim_elsewhere())

    calls = {"n": 0}

    async def counting_patch(list_id, item_id, fields):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(staff_writeback.sp_client, "update_list_item_fields", counting_patch)

    result = run(staff_writeback.drain_writebacks())
    assert result == {"sent": 0, "failed": 0, "skipped": 0}
    assert calls["n"] == 0                                     # the claimed row was skipped

    async def status():
        async with async_session() as session:
            return (await session.execute(select(StaffWriteback))).scalars().first().status
    assert run(status()) == staff_writeback.SENDING            # still in flight, untouched


def test_a_stale_claim_is_reclaimed(clean_db, monkeypatch):
    # A "sending" row whose claim has expired (its owner crashed) is taken again by
    # the next drain and sent, so a crash never strands a change.
    _seed_one(monkeypatch, _sp_item("101"))

    async def stale_claim():
        async with async_session() as session:
            staff_writeback.enqueue_writeback(session, "101", {"CurrentVacationBalance": 1.0})
            await session.commit()
        async with async_session() as session:
            row = (await session.execute(select(StaffWriteback))).scalars().first()
            row.status = staff_writeback.SENDING
            row.next_attempt_at = utcnow() - timedelta(minutes=20)   # claim long expired
            await session.commit()
    run(stale_claim())

    calls = {"n": 0}

    async def counting_patch(list_id, item_id, fields):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(staff_writeback.sp_client, "update_list_item_fields", counting_patch)

    result = run(staff_writeback.drain_writebacks())
    assert result["sent"] == 1 and calls["n"] == 1             # reclaimed and sent

    async def status():
        async with async_session() as session:
            return (await session.execute(select(StaffWriteback))).scalars().first().status
    assert run(status()) == staff_writeback.DONE


# --- Pull: newer wins, logged, never echoed -----------------------------------

def test_pull_applies_a_newer_non_balance_edit_and_logs_it_without_echo(clean_db, monkeypatch):
    # A newer SharePoint edit to a non-balance field is applied, logged with source
    # "sharepoint", and never echoed back. Balances are app-owned, so the unchanged
    # vacation pot stays as it was and no finding is filed.
    _seed_one(monkeypatch, _sp_item("101"))

    edited = _sp_item("101", CellNumber="555-9999", Modified="2026-10-20T09:00:00Z")
    _fake_list(monkeypatch, [edited])

    async def do_pull():
        async with async_session() as session:
            counts = await staff_pull.staff_pull_step(session, None)
            await session.commit()
            return counts
    counts = run(do_pull())
    assert counts["applied"] == 1 and counts["changes_recorded"] >= 1
    assert counts["balance_findings"] == 0

    async def read():
        async with async_session() as session:
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            changes = (await session.execute(
                select(StaffChange).where(StaffChange.employee_id == "101")
            )).scalars().all()
            wb = (await session.execute(select(StaffWriteback))).scalars().all()
            return row.cell_number, float(row.vacation_balance), changes, wb
    cell, vacation, changes, wb = run(read())
    assert cell == "555-9999"                                  # the non-balance edit landed
    assert vacation == 10.5                                    # the app-owned balance is untouched
    assert any(c.field == "CellNumber" and c.source == "sharepoint" for c in changes)
    assert wb == []                                            # a pulled edit is never written back


def test_pull_never_writes_a_balance_and_files_one_finding(clean_db, monkeypatch):
    # A newer SharePoint edit to a balance pot is NOT applied (balances are
    # app-owned); the gap is filed once as a sharepoint_edit_after_switch finding
    # carrying both values, and Postgres is left unchanged.
    _seed_one(monkeypatch, _sp_item("101"))                    # stored vacation 10.5

    edited = _sp_item("101", CurrentVacationBalance=4.0, Modified="2026-10-20T09:00:00Z")
    _fake_list(monkeypatch, [edited])

    async def do_pull():
        async with async_session() as session:
            counts = await staff_pull.staff_pull_step(session, None)
            await session.commit()
            return counts
    counts = run(do_pull())
    assert counts["balance_findings"] == 1

    async def read():
        async with async_session() as session:
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            findings = (await session.execute(
                select(LegacyFinding).where(LegacyFinding.subject_id == "101")
            )).scalars().all()
            wb = (await session.execute(select(StaffWriteback))).scalars().all()
            return float(row.vacation_balance), findings, wb
    vacation, findings, wb = run(read())
    assert vacation == 10.5                                    # the balance is never overwritten
    assert len(findings) == 1
    assert findings[0].case == "sharepoint_edit_after_switch"
    diff = findings[0].evidence["balances"]["CurrentVacationBalance"]
    assert diff["sharepoint"] == 4.0 and diff["postgres"] == 10.5   # both values recorded
    assert wb == []                                            # a finding is never echoed back


def test_pull_leaves_a_person_with_a_pending_writeback_alone(clean_db, monkeypatch):
    # An app change still queued for SharePoint means SharePoint is behind, so the
    # pull leaves the person untouched: no field is overwritten and no finding is
    # filed, even for a balance that differs.
    _seed_one(monkeypatch, _sp_item("101"))                    # stored vacation 10.5, cell 555-1212

    async def queue():
        async with async_session() as session:
            staff_writeback.enqueue_writeback(session, "101", {"CurrentVacationBalance": 10.5})
            await session.commit()
    run(queue())

    edited = _sp_item("101", CurrentVacationBalance=4.0, CellNumber="555-0000",
                      Modified="2026-10-20T09:00:00Z")
    _fake_list(monkeypatch, [edited])

    async def do_pull():
        async with async_session() as session:
            counts = await staff_pull.staff_pull_step(session, None)
            await session.commit()
            return counts
    counts = run(do_pull())
    assert counts["skipped_pending"] == 1
    assert counts["applied"] == 0 and counts["balance_findings"] == 0

    async def read():
        async with async_session() as session:
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            findings = (await session.execute(select(LegacyFinding))).scalars().all()
            return float(row.vacation_balance), row.cell_number, findings
    vacation, cell, findings = run(read())
    assert vacation == 10.5 and cell == "555-1212"             # nothing overwritten
    assert findings == []                                      # SharePoint is behind, not a discrepancy


def test_pull_imports_a_new_item_with_a_duplicate_email_blanked(clean_db, monkeypatch):
    # A new SharePoint item whose email already belongs to someone is imported with
    # a blank email rather than violating the unique index (which would fail the
    # whole tick).
    _seed_one(monkeypatch, _sp_item("101"))                    # Ada holds ada@ucsh.com

    existing = _sp_item("101")                                 # unchanged, older-or-equal -> skipped
    new_dup = _sp_item("202", Title="Clone", EmailAddress="ada@ucsh.com",
                       Modified="2026-10-20T09:00:00Z")
    _fake_list(monkeypatch, [existing, new_dup])

    async def do_pull():
        async with async_session() as session:
            counts = await staff_pull.staff_pull_step(session, None)
            await session.commit()
            return counts
    counts = run(do_pull())
    assert counts["imported"] == 1 and counts["email_conflicts"] == 1
    assert counts["failed"] == 0                               # the clash never failed the run

    async def read():
        async with async_session() as session:
            clone = (await session.execute(
                select(Staff).where(Staff.sp_item_id == "202"))).scalars().first()
            ada = (await session.execute(
                select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            return clone, ada.email
    clone, ada_email = run(read())
    assert clone is not None and clone.email is None           # imported, email blanked to NULL
    assert ada_email == "ada@ucsh.com"                         # the first holder keeps it


def test_pull_keeps_the_newer_stored_edit(clean_db, monkeypatch):
    _seed_one(monkeypatch, _sp_item("101"))     # stored Modified 2026-10-10

    # SharePoint shows an OLDER edit than what is stored: the stored one wins.
    older = _sp_item("101", CurrentVacationBalance=99.0, Modified="2026-01-01T00:00:00Z")
    _fake_list(monkeypatch, [older])

    async def do_pull():
        async with async_session() as session:
            counts = await staff_pull.staff_pull_step(session, None)
            await session.commit()
            return counts
    counts = run(do_pull())
    assert counts["skipped_older"] == 1 and counts["applied"] == 0

    async def read():
        async with async_session() as session:
            row = (await session.execute(select(Staff).where(Staff.sp_item_id == "101"))).scalars().first()
            return float(row.vacation_balance)
    assert run(read()) == 10.5                                 # unchanged


# --- #341 detectors -----------------------------------------------------------

def _cases_for(sp_id):
    """The finding cases filed against one staff id."""
    async def inner():
        async with async_session() as session:
            rows = (await session.execute(
                select(LegacyFinding).where(LegacyFinding.subject_id == str(sp_id))
            )).scalars().all()
            return {r.case for r in rows}
    return run(inner())


def test_employment_type_suspect_fires_and_is_quiet_when_clean(clean_db, monkeypatch):
    suspect = _sp_item("1", SalaryHourly="Hourly", DefaultYearlyVacationDays=10,
                       CurrentVacationBalance=5)
    clean = _sp_item("2", SalaryHourly="Hourly", DefaultYearlyVacationDays=0,
                     CurrentVacationBalance=0, EmailAddress="clean@ucsh.com")
    _fake_list(monkeypatch, [suspect, clean])
    _import([suspect, clean])
    assert "employment_type_suspect" in _cases_for("1")
    assert "employment_type_suspect" not in _cases_for("2")


def test_username_as_name_fires_and_is_quiet_when_clean(clean_db, monkeypatch):
    login = _sp_item("1", Title="jsmith2", EmailAddress="jsmith@ucsh.com")
    person = _sp_item("2", Title="Jane Smith", EmailAddress="jane@ucsh.com")
    _fake_list(monkeypatch, [login, person])
    _import([login, person])
    assert "username_as_name" in _cases_for("1")
    assert "username_as_name" not in _cases_for("2")


def test_no_user_link_fires_and_is_quiet_when_clean(clean_db, monkeypatch):
    no_link = _sp_item("1", TitleLinkLookupId=None)
    linked = _sp_item("2", TitleLinkLookupId=55, EmailAddress="linked@ucsh.com")
    _fake_list(monkeypatch, [no_link, linked])
    _import([no_link, linked])
    assert "no_user_link" in _cases_for("1")
    assert "no_user_link" not in _cases_for("2")


def test_below_zero_predates_rules_fires_and_is_quiet_when_clean(clean_db, monkeypatch):
    # Salaried: vacation may go below zero, but a negative sick predates the rules.
    bad = _sp_item("1", SalaryHourly="Salary", CurrentSickDayBalance=-2,
                   CurrentVacationBalance=-5)
    clean = _sp_item("2", SalaryHourly="Salary", CurrentSickDayBalance=1,
                     CurrentVacationBalance=-5, EmailAddress="clean@ucsh.com")
    _fake_list(monkeypatch, [bad, clean])
    _import([bad, clean])
    assert "below_zero_predates_rules" in _cases_for("1")
    assert "below_zero_predates_rules" not in _cases_for("2")   # only vacation negative: allowed


def test_findings_are_not_duplicated_on_rerun(clean_db, monkeypatch):
    item = _sp_item("1", TitleLinkLookupId=None)
    _fake_list(monkeypatch, [item])
    _import([item])
    _import([item])                                            # rerun

    async def count_no_link():
        async with async_session() as session:
            return (await session.execute(
                select(func.count()).select_from(LegacyFinding)
                .where(LegacyFinding.case == "no_user_link", LegacyFinding.subject_id == "1")
            )).scalar_one()
    assert run(count_no_link()) == 1                           # fingerprint keeps it unique


# --- The staff_storage switch -------------------------------------------------

def test_staff_list_uses_sharepoint_by_default(monkeypatch):
    # Default STORAGE_EMPLOYEES: the read goes straight to sp_client.
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    calls = []

    async def fake_items(list_id, **kwargs):
        calls.append(list_id)
        return []
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_items)
    run(staff_store.get_list_items(STAFF))
    assert calls == [STAFF]


def test_other_lists_always_go_to_sharepoint(monkeypatch):
    # Even pinned to Postgres, a non-staff list id reaches sp_client untouched.
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    calls = []

    async def fake_items(list_id, **kwargs):
        calls.append(list_id)
        return []
    monkeypatch.setattr(sharepoint_module.sp_client, "get_list_items", fake_items)
    run(staff_store.get_list_items(settings.SP_LIST_LEAVE_REQUESTS))
    assert calls == [settings.SP_LIST_LEAVE_REQUESTS]


def test_a_mistyped_staff_setting_fails_loudly(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgress")
    with pytest.raises(NotImplementedError):
        run(staff_store.get_list_items(STAFF))


def test_create_through_the_store_queues_a_writeback(clean_db, monkeypatch):
    # A create while on Postgres mints an id and queues the SharePoint replay.
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    created = run(staff_store.create_list_item(STAFF, {"Title": "New Person", "SalaryHourly": "Salary"}))
    assert created["fields"]["Title"] == "New Person"

    async def read_wb():
        async with async_session() as session:
            return (await session.execute(select(StaffWriteback))).scalars().all()
    wb = run(read_wb())
    assert len(wb) == 1 and wb[0].sp_item_id == created["id"]
