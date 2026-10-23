"""Potential Employee Setup Issues on the manager dashboard (#463).

A manager sees the admin setup screen for their own team only: the list, the
single-person check and Settle requests. These tests pin whose records each
route reaches: the list grades only teammates (never the manager's own record,
never an archived person), the single check and the settle routes refuse anyone
not on the team before anything runs (a missing request and someone else's
answer alike, so nothing about another person's request is revealed), the
manager's own record and requests are refused like the profile edit refuses
them, and a settle is recorded under the manager's name. An employee token
cannot reach the list at all. One service test pins the sweep's ``keep``
filter: only the team is graded and counted, while the duplicate-name check
still compares against the whole directory.

The routes are plain async functions, called directly with a constructed
``DashboardUser``; the sweep, the staff reads and the settle service are faked,
so no database or SharePoint is touched.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.routes.dashboard import DashboardUser
from app.routes import team_manage as tm
from app.services import employee_validation as ev


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _record(item_id: str, name: str, managers: list[str], **fields) -> dict:
    """A staff record in the ``{"id", "fields"}`` shape the routes read.

    Args:
        item_id: The staff id.
        name: The person's display name.
        managers: Their managers' names (AllManagers).
        **fields: Extra fields, e.g. ``ArchivedAt``.

    Returns:
        The record.
    """
    entries = [{"LookupId": 0, "LookupValue": m} for m in managers]
    return {"id": item_id, "fields": {"Title": name, "AllManagers": entries, **fields}}


STEVE = _record("42", "Steve Faith", ["Steve Faith"])                     # lists himself, oddly
ROLAND = _record("480", "Roland Alvarez", ["Steve Faith"])                # Steve's
OMAR = _record("482", "Omar Diaz", ["Dana Quinn"])                        # Dana's, not Steve's
GONE = _record("490", "Gina Old", ["Steve Faith"], ArchivedAt="2026-09-01T00:00:00Z")   # archived
STAFF = {r["id"]: r for r in (STEVE, ROLAND, OMAR, GONE)}

STEVE_USER = DashboardUser(role="manager", user_id="42")


@pytest.fixture
def faked(monkeypatch):
    """Steve signed in, staff read from STAFF, the sweep and the settle service faked.

    Yields:
        A dict collecting what the fakes were called with.
    """
    calls: dict = {"settled": [], "validated": []}

    async def signed_in(user):
        return STAFF["42"], "Steve Faith"

    async def by_id(employee_id):
        return STAFF.get(str(employee_id))

    async def sweep(keep=None):
        kept = [r for r in STAFF.values() if keep is None or keep(r)]    # what the route asked to grade
        calls["kept"] = [r["id"] for r in kept]
        return {"total_checked": len(kept), "flagged": [], "directory_unreadable": False}

    async def validate(employee_id):
        calls["validated"].append(str(employee_id))
        return {"employee_id": str(employee_id), "overall": "pass", "checks": []}

    async def owner(kind, item_id):
        # 7001 is Roland's, 7002 Omar's, 7003 Steve's own; anything else does not exist.
        return {"7001": "480", "7002": "482", "7003": "42"}.get(str(item_id))

    async def preview(kind, item_id):
        calls["previewed"] = str(item_id)
        return {"request": {}, "pots": {}, "plan": {}, "hand_editable": False}

    async def settle(kind, item_id, outcome, reason, pieces):
        calls["settled"].append((str(item_id), outcome, reason))
        return {"status": "settled", "outcome": outcome, "balance_state": "applied", "moves": []}

    monkeypatch.setattr(tm, "_signed_in_manager", signed_in)
    monkeypatch.setattr(tm, "get_employee_by_id", by_id)
    monkeypatch.setattr(tm, "validate_all_employee_setups", sweep)
    monkeypatch.setattr(tm, "validate_employee_setup", validate)
    monkeypatch.setattr(tm, "_request_owner", owner)
    monkeypatch.setattr(tm.balance_settle, "settle_preview", preview)
    monkeypatch.setattr(tm.balance_settle, "settle", settle)
    monkeypatch.setattr(tm.settings, "PROCESSING_ENABLED", True)
    return calls


def test_the_list_grades_only_the_managers_team(faked):
    # Roland is graded; Steve's own record, Dana's Omar and archived Gina are not.
    result = run(tm.team_employee_setup(STEVE_USER))
    assert faked["kept"] == ["480"]
    assert result["total_checked"] == 1                                    # the count is the team's


def test_an_employee_token_cannot_read_the_team_list():
    # The real role guard refuses before any staff read.
    with pytest.raises(HTTPException) as err:
        run(tm.team_employee_setup(DashboardUser(role="employee", user_id="480")))
    assert err.value.status_code == 403


def test_the_single_check_runs_for_a_teammate(faked):
    assert run(tm.team_validate_employee(STEVE_USER, "480"))["overall"] == "pass"
    assert faked["validated"] == ["480"]


def test_the_single_check_is_refused_for_a_non_teammate(faked):
    with pytest.raises(HTTPException) as err:
        run(tm.team_validate_employee(STEVE_USER, "482"))
    assert err.value.status_code == 403
    assert faked["validated"] == []                                        # nothing was read for Omar


def test_a_settle_is_recorded_under_the_managers_name(faked):
    body = tm.TeamSettleBody(outcome="now", reason="approved before the switch")
    result = run(tm.team_settle_request(STEVE_USER, "leave", "7001", body))
    assert result["status"] == "settled"
    assert faked["settled"] == [("7001", "now", "Settled by Steve Faith: approved before the switch")]


def test_a_settle_is_refused_for_a_non_teammates_request(faked):
    body = tm.TeamSettleBody(outcome="now", reason="not mine")
    with pytest.raises(HTTPException) as err:
        run(tm.team_settle_request(STEVE_USER, "leave", "7002", body))
    assert err.value.status_code == 404                                    # same answer as a missing request
    assert faked["settled"] == []                                          # nothing was written


def test_a_non_teammates_request_reads_like_a_missing_one(faked):
    # Omar's request and a request that does not exist get the same answer, and
    # the settle service is never asked about either.
    answers = []
    for item in ("7002", "9999"):
        with pytest.raises(HTTPException) as err:
            run(tm.team_settle_preview(STEVE_USER, "leave", item))
        answers.append((err.value.status_code, err.value.detail))
    assert answers[0] == answers[1] == (404, tm.REQUEST_NOT_ON_TEAM)
    assert "previewed" not in faked


def test_the_preview_runs_for_a_teammates_request(faked):
    run(tm.team_settle_preview(STEVE_USER, "leave", "7001"))
    assert faked["previewed"] == "7001"


def test_a_manager_cannot_check_or_settle_their_own_record(faked):
    # Steve lists himself, so he passes the team check; his own record is refused anyway.
    with pytest.raises(HTTPException) as err:
        run(tm.team_validate_employee(STEVE_USER, "42"))
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        run(tm.team_settle_request(STEVE_USER, "leave", "7003", tm.TeamSettleBody(outcome="none", reason="mine")))
    assert err.value.status_code == 403
    assert faked["validated"] == [] and faked["settled"] == []


def test_a_settle_needs_a_reason(faked):
    with pytest.raises(HTTPException) as err:
        run(tm.team_settle_request(STEVE_USER, "leave", "7001", tm.TeamSettleBody(outcome="now", reason="  ")))
    assert err.value.status_code == 400
    assert faked["settled"] == []


def test_the_sweep_grades_only_kept_records_but_checks_names_company_wide(monkeypatch):
    # Two Roland Alvarez records: one on Steve's team, one on Dana's. Only the
    # team's record is graded and counted, and it still fails the shared-name
    # check because the other Roland is in the directory.
    def person(item_id, manager):
        rec = _record(item_id, "Roland Alvarez", [manager])
        rec["fields"].update({"EmailAddress": f"roland{item_id}@ucsh.com", "Location": "Toronto Warden"})
        return rec

    staff = [person("480", "Steve Faith"), person("481", "Dana Quinn")]

    async def list_items(list_id, **kwargs):
        return staff

    class NoHolidays:
        async def get_all(self):
            return []

    async def on_postgres():
        return True

    async def nothing_unmoved(employee_id=None):
        return {}

    monkeypatch.setattr(ev.staff_store, "get_list_items", list_items)
    monkeypatch.setattr(ev, "get_holiday_repository", lambda: NoHolidays())
    monkeypatch.setattr(ev, "staff_in_postgres", on_postgres)
    monkeypatch.setattr(ev, "approved_requests_without_balance_change", nothing_unmoved)

    result = run(ev.validate_all_employee_setups(keep=lambda r: r["id"] == "480"))
    assert result["total_checked"] == 1                                    # only the team's record counted
    assert [row["employee_id"] for row in result["flagged"]] == ["480"]    # the other Roland is not listed
    codes = {f["code"] for f in result["flagged"][0]["fails"]}
    assert "identity_unique_name" in codes                                 # the name check saw both records
