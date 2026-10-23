"""Potential Employee Setup Issues on the manager dashboard (#463).

A manager sees the admin setup screen for their own team only: the list, the
single-person check and Settle requests. These tests pin whose records each
route reaches: the list grades only teammates (never the manager's own record,
never an archived person), the single check and the settle routes refuse anyone
not on the team before anything runs, and a settle is recorded under the
manager's name. An employee token cannot reach the list at all.

The routes are plain async functions, called directly with a constructed
``DashboardUser``; the sweep, the staff reads and the settle service are faked,
so no database or SharePoint is touched.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.routes.dashboard import DashboardUser
from app.routes import team_manage as tm


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

    async def preview(kind, item_id):
        owner = {"7001": "480", "7002": "482"}[str(item_id)]             # 7001 is Roland's, 7002 Omar's
        return {"request": {"employee_id": owner}, "pots": {}, "plan": {}, "hand_editable": False}

    async def settle(kind, item_id, outcome, reason, pieces):
        calls["settled"].append((str(item_id), outcome, reason))
        return {"status": "settled", "outcome": outcome, "balance_state": "applied", "moves": []}

    monkeypatch.setattr(tm, "_signed_in_manager", signed_in)
    monkeypatch.setattr(tm, "get_employee_by_id", by_id)
    monkeypatch.setattr(tm, "validate_all_employee_setups", sweep)
    monkeypatch.setattr(tm, "validate_employee_setup", validate)
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
    assert err.value.status_code == 403
    assert faked["settled"] == []                                          # nothing was written


def test_the_settle_preview_is_refused_for_a_non_teammates_request(faked):
    with pytest.raises(HTTPException) as err:
        run(tm.team_settle_preview(STEVE_USER, "leave", "7002"))
    assert err.value.status_code == 403


def test_a_settle_needs_a_reason(faked):
    with pytest.raises(HTTPException) as err:
        run(tm.team_settle_request(STEVE_USER, "leave", "7001", tm.TeamSettleBody(outcome="now", reason="  ")))
    assert err.value.status_code == 400
    assert faked["settled"] == []
