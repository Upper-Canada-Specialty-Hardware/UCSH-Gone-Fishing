"""Tests for the Staff Directory version-history backfill (#448).

The pure planner (``plan_person_rows``) turns SharePoint versions into history
rows; the writer (``_write_person``) persists them re-runnably; and the Graph
cut-off exception lets only the Staff Directory versions read through after the
switch. All run against the local SQLite database conftest builds with
create_all, with secrets stubbed and no network (versions are hand-built dicts).
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.staff_change import StaffChange
from app.services import sharepoint_cutoff as co
from app.services import staff_changes
from app.services import staff_history_import as step

STAFF = settings.SP_LIST_STAFF_DIRECTORY
T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def run(coro):
    """Run one coroutine on a fresh event loop."""
    return asyncio.run(coro)


def version(n, minutes, by="Human Resources", comment=None, **fields):
    """One Graph listItemVersion, ``minutes`` after T0, with the given fields."""
    at = (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    body = dict(fields)
    if comment is not None:                                    # only set Comments when the test wants one
        body["Comments"] = comment
    return {"id": f"{n}.0", "lastModifiedDateTime": at,
            "lastModifiedBy": {"user": {"displayName": by}}, "fields": body}


async def _wipe():
    """Empty the two tables these tests touch."""
    async with async_session() as session:
        await session.execute(delete(StaffChange))
        await session.execute(delete(BalanceLedger))
        await session.commit()


async def _rows(employee_id):
    """Every staff_changes row for one person, oldest first."""
    async with async_session() as session:
        return (await session.execute(
            select(StaffChange).where(StaffChange.employee_id == str(employee_id))
            .order_by(StaffChange.id)
        )).scalars().all()


@pytest.fixture(autouse=True)
def clean():
    """Empty both tables before and after every test."""
    run(_wipe())
    yield
    run(_wipe())


# ----- the pure planner -----

def test_starting_rows_seed_each_present_field_from_the_oldest_version():
    versions = [version(1, 0, CurrentVacationBalance=10, DefaultYearlyVacationDays=15, SalaryHourly="Salary")]
    by_field = {r["field"]: r for r in step.plan_person_rows(versions, set())}
    assert by_field["CurrentVacationBalance"]["old_value"] is None       # a starting row has no "before"
    assert by_field["CurrentVacationBalance"]["new_value"] == 10
    assert by_field["CurrentVacationBalance"]["version"] == "1.0"         # from the oldest version
    assert by_field["DefaultYearlyVacationDays"]["new_value"] == 15
    assert by_field["SalaryHourly"]["new_value"] == "Salary"
    assert "CarryOver" not in by_field                                    # absent in the oldest -> no starting row


def test_a_change_row_names_the_editor_and_the_moved_values():
    versions = [version(1, 0, CurrentVacationBalance=10),
                version(2, 10, CurrentVacationBalance=7, by="Jay Puzon")]
    change = [r for r in step.plan_person_rows(versions, set()) if r["version"] == "2.0"]
    assert len(change) == 1
    assert change[0]["field"] == "CurrentVacationBalance"
    assert change[0]["old_value"] == 10 and change[0]["new_value"] == 7
    assert change[0]["actor"] == "Jay Puzon"                              # the SharePoint editor
    assert change[0]["reason"] is None                                    # no comment explained it


def test_a_sharepoint_app_version_is_skipped_but_a_later_human_edit_is_kept():
    versions = [version(1, 0, CurrentVacationBalance=10, DefaultYearlyVacationDays=15),
                version(2, 10, CurrentVacationBalance=9, DefaultYearlyVacationDays=15, by="SharePoint App"),
                version(3, 20, CurrentVacationBalance=9, DefaultYearlyVacationDays=16, by="Jay Puzon")]
    changes = [(r["field"], r["version"]) for r in step.plan_person_rows(versions, set()) if r["old_value"] is not None]
    assert ("DefaultYearlyVacationDays", "3.0") in changes                # the human entitlement edit is kept
    assert all(field != "CurrentVacationBalance" for field, _ in changes)  # the app's balance move is skipped


def test_a_balance_move_matched_to_an_approval_is_dropped_but_entitlement_kept():
    versions = [version(1, 0, CurrentVacationBalance=10, DefaultYearlyVacationDays=15),
                version(2, 10, CurrentVacationBalance=7, DefaultYearlyVacationDays=16, by="Jay Puzon")]
    # Version 2 was matched to an approved request by the balance import.
    fields = {r["field"] for r in step.plan_person_rows(versions, {2.0}) if r["version"] == "2.0"}
    assert "CurrentVacationBalance" not in fields                         # shown through the request, not here
    assert "DefaultYearlyVacationDays" in fields                          # an entitlement edit always stays


def test_the_reason_is_a_comment_edited_in_the_same_version():
    versions = [version(1, 0, CurrentVacationBalance=10),
                version(2, 10, CurrentVacationBalance=7, by="Jay", comment="used 3 days")]
    change = next(r for r in step.plan_person_rows(versions, set())
                  if r["version"] == "2.0" and r["field"] == "CurrentVacationBalance")
    assert change["reason"] == "used 3 days"


def test_the_reason_can_come_from_the_next_version_within_an_hour():
    versions = [version(1, 0, CurrentVacationBalance=10),
                version(2, 10, CurrentVacationBalance=7, by="Jay"),             # the change, no comment yet
                version(3, 40, CurrentVacationBalance=7, by="Jay", comment="used 3 days")]  # comment 30 min later
    change = next(r for r in step.plan_person_rows(versions, set())
                  if r["version"] == "2.0" and r["field"] == "CurrentVacationBalance")
    assert change["reason"] == "used 3 days"


def test_a_comment_too_late_or_by_another_person_is_not_the_reason():
    too_late = [version(1, 0, CurrentVacationBalance=10),
                version(2, 10, CurrentVacationBalance=7, by="Jay"),
                version(3, 100, CurrentVacationBalance=7, by="Jay", comment="90 minutes later")]
    assert next(r for r in step.plan_person_rows(too_late, set())
                if r["version"] == "2.0" and r["field"] == "CurrentVacationBalance")["reason"] is None
    other_person = [version(1, 0, CurrentVacationBalance=10),
                    version(2, 10, CurrentVacationBalance=7, by="Jay"),
                    version(3, 20, CurrentVacationBalance=7, by="Mandy", comment="not Jay's note")]
    assert next(r for r in step.plan_person_rows(other_person, set())
                if r["version"] == "2.0" and r["field"] == "CurrentVacationBalance")["reason"] is None


def test_the_managers_column_reads_as_a_name_list():
    v = version(1, 0, AllManagers=[{"LookupId": 1, "LookupValue": "Boss Person"},
                                   {"LookupId": 2, "LookupValue": "Deputy Boss"}])
    row = next(r for r in step.plan_person_rows([v], set()) if r["field"] == "AllManagers")
    assert row["new_value"] == "Boss Person, Deputy Boss"


# ----- the writer (DB) -----

def test_a_rerun_replaces_only_the_sharepoint_rows():
    async def seed():
        async with async_session() as session:
            session.add(StaffChange(employee_id="7", employee_name="Dana Lee", field="CurrentVacationBalance",
                                    old_value=12, new_value=10, reason="HR edit", source="admin_dashboard"))
            session.add(StaffChange(employee_id="7", employee_name="Dana Lee", field="Payout",
                                    old_value=None, new_value=1, reason=None, source="sharepoint", version="9.9"))
            await session.commit()
    run(seed())
    written = run(step._write_person("7", "Dana Lee", [version(1, 0, CurrentVacationBalance=10)]))
    assert written == 1                                                   # one starting row
    rows = run(_rows("7"))
    assert sorted(r.source for r in rows) == ["admin_dashboard", "sharepoint"]  # the admin row survived
    sp = [r for r in rows if r.source == "sharepoint"]
    assert len(sp) == 1 and sp[0].field == "CurrentVacationBalance" and sp[0].version == "1.0"  # stale row gone


def test_a_ledger_matched_balance_version_is_dropped_end_to_end():
    async def seed_ledger():
        async with async_session() as session:
            session.add(BalanceLedger(                                    # history key naming staff version 2
                employee_id="7", pot="vacation", change=-3, before=10, after=7,
                channel="import", idempotency_key="history:leave:5:2:vacation",
            ))
            await session.commit()
    run(seed_ledger())
    versions = [version(1, 0, CurrentVacationBalance=10, DefaultYearlyVacationDays=15),
                version(2, 10, CurrentVacationBalance=7, DefaultYearlyVacationDays=16, by="Jay Puzon")]
    run(step._write_person("7", "Dana Lee", versions))
    change = {r.field for r in run(_rows("7")) if r.version == "2.0"}
    assert "CurrentVacationBalance" not in change                         # dropped: the approval shows it
    assert "DefaultYearlyVacationDays" in change                          # entitlement kept


def test_an_app_edit_records_its_actor():
    run(staff_changes.record_changes("7", "Dana Lee", [("CurrentVacationBalance", 10, 7)], "fix", actor="HR"))
    row = run(staff_changes.list_changes("7"))[0]
    assert row["actor"] == "HR"                                           # recorded on the app edit
    assert row["version"] is None                                        # app edits carry no SharePoint version


# ----- the cut-off exception -----

def test_the_predicate_allows_only_the_staff_versions_read():
    vpath = f"/sites/abc/lists/{STAFF}/items/7/versions"
    assert co.is_staff_history_read("GET", vpath) is True                 # the staff versions read
    assert co.is_staff_history_read("GET", vpath + "?$skiptoken=x") is True  # a paged follow-up
    assert co.is_staff_history_read("GET", co._SITE_LOOKUP) is True       # the site-id lookup it needs
    assert co.is_staff_history_read("POST", vpath) is False               # never a write
    other = f"/sites/abc/lists/{settings.SP_LIST_LEAVE_REQUESTS}/items/7/versions"
    assert co.is_staff_history_read("GET", other) is False                # not another list


def test_a_query_string_cannot_smuggle_a_read_past_the_gate():
    """A versions path hidden in the query string stays refused, the real path decides (#455)."""
    # A non-versions read of another list, with the staff versions path tucked in
    # the query string: the query is stripped, so the real path does not match.
    smuggle = f"/sites/abc/lists/{settings.SP_LIST_LEAVE_REQUESTS}/items/7?p=/lists/{STAFF}/items/7/versions"
    assert co.is_staff_history_read("GET", smuggle) is False
    # The site lookup must match exactly, not merely be a prefix of a longer path.
    assert co.is_staff_history_read("GET", co._SITE_LOOKUP + "/drive/root") is False
    # An absolute @odata.nextLink to the staff versions still matches on its tail.
    nextlink = f"https://graph.microsoft.com/v1.0/sites/s/lists/{STAFF}/items/7/versions?$skiptoken=abc"
    assert co.is_staff_history_read("GET", nextlink) is True


# ----- the run isolates one person's failure -----


def test_run_import_records_one_persons_failure_and_finishes_the_rest(monkeypatch):
    """One person's write failure is recorded and the others still import (#448)."""
    async def _people():
        return [("1", "Ann"), ("2", "Bob")]

    async def _versions(item_id):
        return [version(1, 0, CurrentVacationBalance=10)]

    async def _write(employee_id, name, versions):
        if employee_id == "2":                                 # Bob's write blows up
            raise RuntimeError("boom")
        return 1                                                # Ann writes one row

    monkeypatch.setattr(step, "_staff_people", _people)
    monkeypatch.setattr(step, "_read_versions", _versions)
    monkeypatch.setattr(step, "_write_person", _write)
    monkeypatch.setattr(step.sp_client, "site_id", "site-123")  # skip the site resolve

    result = run(step.run_import())
    assert result["result"] == "done"                          # the run completed despite the failure
    assert result["people_total"] == 2
    assert result["people_done"] == 2                          # both counted, pass or fail
    assert result["rows_written"] == 1                         # only Ann's row landed
    assert [e["employee_id"] for e in result["errors"]] == ["2"]   # only Bob's failure recorded
    assert "boom" in result["errors"][0]["error"]


def test_the_gate_lets_only_the_allowed_read_through_after_the_switch(monkeypatch):
    from app.graph import auth as graph_auth
    from app.graph import client as graph_client_module

    async def cut_off():
        return True                                                       # pretend the switch has run
    monkeypatch.setattr(co, "sharepoint_cut_off", cut_off)

    class _Resp:
        status_code, content, headers, text = 200, b"{}", {}, "{}"
        def json(self):
            return {"value": []}
        def raise_for_status(self):
            return None

    sent = []
    async def fake_request(method, path, headers=None, **kwargs):
        sent.append((method, path))                                       # record what actually went out
        return _Resp()
    monkeypatch.setattr(graph_client_module.graph_client, "_http", types.SimpleNamespace(request=fake_request))

    async def fake_token():
        return "token"
    monkeypatch.setattr(graph_auth.token_manager, "get_token", fake_token)

    vpath = f"/sites/s/lists/{STAFF}/items/7/versions"
    run(graph_client_module.graph_client.get(vpath, allow_after_switch=True))  # allowed read goes out
    assert sent[-1] == ("GET", vpath)

    with pytest.raises(co.SharePointCutOff):                              # a write stays refused, flag or not
        run(graph_client_module.graph_client._request("POST", vpath, allow_after_switch=True))
    other = f"/sites/s/lists/{settings.SP_LIST_LEAVE_REQUESTS}/items/7/versions"
    with pytest.raises(co.SharePointCutOff):                              # another list's versions stay refused
        run(graph_client_module.graph_client.get(other, allow_after_switch=True))
    with pytest.raises(co.SharePointCutOff):                              # the same read without the flag is refused
        run(graph_client_module.graph_client.get(vpath))
