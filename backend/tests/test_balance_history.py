"""Old approvals' balance moves and field history, from SharePoint versions (#357).

The pure matcher (balance_history) and the sync step that writes what it finds
(balance_history_import). Versions are built the way Graph returns them.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models.balance_ledger import BalanceLedger
from app.models.request_change import RequestChange
from app.models.request_record import RequestRecord
from app.services import balance_history as bh
from app.services import balance_history_import as step

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def run(coro):
    """Run one coroutine on a fresh event loop."""
    return asyncio.run(coro)


def version(n, minutes, by="Human Resources", **fields):
    """One Graph listItemVersion, ``minutes`` after T0."""
    at = (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return {"id": f"{n}.0", "lastModifiedDateTime": at,
            "lastModifiedBy": {"user": {"displayName": by}}, "fields": fields}


def staff(n, minutes, vac=5.0, sick=3.0, mu=0.0, co=0.0, po=0.0, by="SharePoint App"):
    """One Staff Directory version with the five pots."""
    return version(n, minutes, by, CurrentVacationBalance=vac, CurrentSickDayBalance=sick,
                   CurrentOvertimeBalance=mu, CarryOver=co, Payout=po)


def leave(days, status="approved", **more):
    """A leave request as the matcher reads it."""
    return {"kind": "leave", "status": status, "days": days, "hours": None, "request_option": None,
            "start_date": more.get("start_date"), "decided_at": None, "created_at": T0 - timedelta(days=3)}


# ----- the pure matcher -----

def test_steps_are_the_writes_that_moved_a_pot_in_version_order():
    versions = [staff(3, 20, vac=4), staff(1, 0), staff(2, 10, by="HR", vac=5, sick=3)]   # out of order on purpose
    steps = bh.balance_steps(versions)
    assert len(steps) == 1                                     # v1 -> v2 moved nothing
    assert steps[0].version == 3.0 and steps[0].deltas == {"vacation": -1.0}


def test_the_approval_moment_is_when_status_first_read_approved():
    versions = [version(1, 0, Status="Pending"), version(2, 5, by="Dave Powell", Status="Approved"),
                version(3, 9, Status="Approved")]
    at, by = bh.status_moment(versions, "Approved")
    assert at == T0 + timedelta(minutes=5) and by == "Dave Powell"


def test_field_history_leaves_out_sharepoint_bookkeeping():
    versions = [version(1, 0, Status="Pending", Modified="x", _UIVersionString="1.0"),
                version(2, 5, by="Dave Powell", Status="Approved", Modified="y", _UIVersionString="2.0")]
    [change] = bh.field_changes(versions)
    assert (change["field"], change["old"], change["new"], change["by"]) == ("Status", "Pending", "Approved", "Dave Powell")


@pytest.mark.parametrize("request_, net, expected", [
    (leave(2), {"vacation": -1.5, "make_up": -0.5}, True),          # leave across two pots
    (leave(2), {"vacation": -2, "sick": 1}, False),                 # something went back: not this leave
    ({"kind": "overtime", "hours": 8}, {"make_up": 1}, True),
    ({"kind": "overtime", "hours": 8}, {"vacation": 1}, True),      # all of it paid off negative vacation
    ({"kind": "overtime", "hours": 8}, {"vacation": 1.5, "make_up": -0.5}, True),   # used old make-up too
    ({"kind": "carryover_payout", "days": 3, "request_option": "carry_over"}, {"vacation": -3, "carry_over": 3}, True),
    ({"kind": "carryover_payout", "days": 3, "request_option": "payout"}, {"vacation": -3, "carry_over": 3}, False),
])
def test_each_kind_has_its_own_shape(request_, net, expected):
    assert bh.looks_like_approval(request_, net) is expected


def test_an_approval_is_matched_to_the_write_at_its_moment():
    item = [version(1, 0, Status="Pending"), version(2, 60, by="Dave Powell", Status="Approved")]
    steps = bh.balance_steps([staff(1, 0), staff(2, 30, vac=4), staff(3, 61, vac=3)])   # a 1-day write earlier, too
    used: set[int] = set()
    match = bh.match_request(leave(1), item, steps, used)
    assert [s.version for s in match.approval] == [3.0]        # the one at the approval, not the earlier one
    assert match.approved_by == "Dave Powell" and used == {1}


def test_an_old_flow_that_moved_pots_in_several_quick_writes_is_one_approval():
    # Make-Up -2, then Make-Up +2 and carry-over -2, then vacation -2 and carry-over +2: net vacation -2.
    item = [version(1, 0, Status="Pending"), version(2, 60, Status="Approved")]
    steps = bh.balance_steps([staff(1, 0, vac=5, mu=0, co=0), staff(2, 60, vac=5, mu=-2),
                              staff(3, 60.02, vac=5, mu=0, co=-2), staff(4, 60.05, vac=3, mu=0, co=0)])
    match = bh.match_request(leave(2), item, steps, set())
    assert [s.version for s in match.approval] == [2.0, 3.0, 4.0]


def test_a_refund_finds_its_mirror_image_too():
    item = [version(1, 0, Status="Pending"), version(2, 10, Status="Approved"), version(3, 300, Status="Refunded")]
    steps = bh.balance_steps([staff(1, 0), staff(2, 10, vac=3), staff(3, 300, vac=5)])
    match = bh.match_request(leave(2, status="refunded"), item, steps, set())
    assert [s.version for s in match.refund] == [3.0]


def test_two_requests_never_claim_one_write():
    item = [version(1, 0, Status="Pending"), version(2, 10, Status="Approved")]
    steps = bh.balance_steps([staff(1, 0), staff(2, 10, vac=4)])
    used: set[int] = set()
    assert bh.match_request(leave(1), item, steps, used) is not None
    assert bh.match_request(leave(1), item, steps, used) is None


def test_a_leave_older_than_its_history_matches_by_its_first_day_only_when_one_write_fits():
    born = [version(214, 0, Status="Approved")]                # its history starts after it was approved
    start = (T0 + timedelta(days=10)).date()
    one = bh.balance_steps([staff(1, -600), staff(2, 60 * 24, vac=4)])          # one 1-day write before the start
    assert bh.match_request(leave(1, start_date=start), born, one, set()).approved_by is None
    two = bh.balance_steps([staff(1, -600), staff(2, 60 * 24, vac=4), staff(3, 60 * 48, vac=3)])
    assert bh.match_request(leave(1, start_date=start), born, two, set()) is None
    assert bh.gap_reason(leave(1, start_date=start), born, two, set()) == bh.GAP_AMBIGUOUS


@pytest.mark.parametrize("steps, versions, expected", [
    ([], [version(1, 0, Status="Approved")], bh.GAP_NO_STAFF_RECORD),
    (bh.balance_steps([staff(1, 60 * 24 * 10), staff(2, 60 * 24 * 11, vac=4)]),       # history begins 10 days on
     [version(1, 0, Status="Pending"), version(2, 50, Status="Approved")], bh.GAP_BEFORE_RECORDS),
    (bh.balance_steps([staff(1, 0), staff(2, 30, vac=1)]),
     [version(1, 0, Status="Pending"), version(2, 50, Status="Approved")], bh.GAP_NO_MATCH),
])
def test_a_miss_says_why(steps, versions, expected):
    assert bh.match_request(leave(1), versions, steps, set()) is None
    assert bh.gap_reason(leave(1), versions, steps, set()) == expected


# ----- the sync step -----

@pytest.fixture
def clean():
    """Empty the request, ledger and change tables around each step test."""
    async def wipe():
        async with async_session() as session:
            await session.execute(delete(RequestChange))
            await session.execute(delete(BalanceLedger))
            await session.execute(delete(RequestRecord))
            await session.commit()
    run(wipe())
    yield
    run(wipe())


def _seed(item_id, status="approved", balance_state="not_applied", days=2.0, employee_id="42"):
    """One imported leave whose SharePoint item is at version 3.0."""
    async def go():
        async with async_session() as session:
            session.add(RequestRecord(
                kind="leave", item_id=item_id, sp_item_id=str(item_id), status=status, employee_id=employee_id,
                employee_name="Pat Staff", leave_type="Vacation", start_date=date(2026, 9, 10), days=days,
                balance_state=balance_state, created_at=T0 - timedelta(days=2),
                extra={"sp_version": "3.0"},
            ))
            await session.commit()
    run(go())


def _fake_sharepoint(monkeypatch, staff_versions, item_versions):
    """Answer get_item_versions from fixtures, and count the reads."""
    reads = []

    async def get_item_versions(list_id, item_id):
        reads.append(str(item_id))
        return staff_versions if list_id == step.settings.SP_LIST_STAFF_DIRECTORY else item_versions.get(str(item_id), [])
    monkeypatch.setattr(step.sp_client, "get_item_versions", get_item_versions)
    return reads


def _import(budget=60):
    """Run the step's work in one session and commit, as a sync does."""
    async def go():
        async with async_session() as session:
            counts = await step.import_history(session, budget)
            await session.commit()
            return counts
    return run(go())


def _row(item_id):
    """The stored request, its ledger rows and its field-history rows."""
    async def go():
        async with async_session() as session:
            record = (await session.execute(select(RequestRecord).where(RequestRecord.item_id == item_id))).scalar_one()
            ledger = (await session.execute(select(BalanceLedger).where(BalanceLedger.request_id == record.id))).scalars().all()
            changes = (await session.execute(select(RequestChange).where(RequestChange.request_id == record.id))).scalars().all()
            return record, ledger, changes
    return run(go())


def test_the_step_writes_the_approvals_moves_and_its_field_history(clean, monkeypatch):
    _seed(7001)
    _fake_sharepoint(
        monkeypatch,
        staff_versions=[staff(1, 0, vac=5, mu=1), staff(2, 30, by="SharePoint App", vac=4, mu=0)],
        item_versions={"7001": [version(1, 0, Status="Pending"), version(3, 30, by="Dave Powell", Status="Approved")]},
    )
    counts = _import()
    record, ledger, changes = _row(7001)

    assert counts["matched"] == 1 and counts["remaining"] == 0
    assert record.balance_state == "applied"
    assert record.decided_at is not None and record.decided_by == "Dave Powell"
    assert sorted((r.pot, float(r.change)) for r in ledger) == [("make_up", -1.0), ("vacation", -1.0)]
    assert all(r.actor == "Dave Powell" and r.channel == "import" for r in ledger)
    [status] = [c for c in changes if c.field == "Status"]
    assert (status.old_value, status.new_value, status.actor) == ("Pending", "Approved", "Dave Powell")
    assert record.extra["sp_history"] == {"version": "3.0", "balance": "matched"}


def test_an_unchanged_item_is_not_read_again(clean, monkeypatch):
    _seed(7002, status="pending", balance_state="not_applied")
    reads = _fake_sharepoint(monkeypatch, [], {"7002": [version(1, 0, Status="Pending")]})
    _import()
    _import()                                                  # same sp_version: nothing to read
    assert reads == ["7002"]


def test_a_miss_is_kept_with_its_reason(clean, monkeypatch):
    _seed(7003, employee_id="spuser:12")                       # nobody on staff to read
    _fake_sharepoint(monkeypatch, [], {"7003": [version(1, 0, Status="Pending"), version(3, 5, Status="Approved")]})
    counts = _import()
    record, ledger, _changes = _row(7003)
    assert counts["not_matched_no_staff_record"] == 1 and ledger == []
    assert record.balance_state == "not_applied" and record.extra["sp_history"]["balance"] == "no_staff_record"


def test_the_final_sync_refuses_the_switch_when_history_is_not_caught_up(clean, monkeypatch):
    _seed(7004, status="pending")
    _fake_sharepoint(monkeypatch, [], {})
    monkeypatch.setattr(step, "FINAL_BUDGET_SECONDS", 0)       # no time at all: everything is left

    async def go():
        async with async_session() as session:
            await step.history_sync_step(session, type("Run", (), {"job": "final_sync"})())
    with pytest.raises(step.HistoryNotCaughtUp, match="1 requests left"):
        run(go())


def test_an_item_edited_after_its_match_gets_fresh_field_history_and_no_second_ledger(clean, monkeypatch):
    _seed(7005)
    item = [version(1, 0, Status="Pending"), version(3, 30, by="Dave Powell", Status="Approved")]
    _fake_sharepoint(monkeypatch, [staff(1, 0), staff(2, 30, vac=3)], {"7005": item})
    _import()

    async def edited_in_sharepoint():                          # the importer saw version 4.0
        async with async_session() as session:
            record = (await session.execute(select(RequestRecord).where(RequestRecord.item_id == 7005))).scalar_one()
            record.extra = {**record.extra, "sp_version": "4.0"}
            await session.commit()
    run(edited_in_sharepoint())
    _fake_sharepoint(monkeypatch, [staff(1, 0), staff(2, 30, vac=3)],
                     {"7005": item + [version(4, 90, by="Mandy Leong", Status="Approved", Comments="fixed")]})
    _import()
    record, ledger, changes = _row(7005)

    assert len(ledger) == 1                                    # matched once; the edit adds no balance move
    assert sorted(c.field for c in changes) == ["Comments", "Status"]   # rewritten, not doubled
    assert record.extra["sp_history"] == {"version": "4.0", "balance": "matched"}


# ----- the Graph reader -----

class _Response:
    """Just enough of an httpx response for raise_for_status callers."""

    def __init__(self, status):
        self.status_code = status


def test_versions_follow_every_page(monkeypatch):
    from app.graph import sharepoint
    pages = iter([{"value": [{"id": "2.0"}], "@odata.nextLink": "https://next"}, {"value": [{"id": "1.0"}]}])
    seen = []

    async def get(url, params=None):
        seen.append(url)
        return next(pages)
    monkeypatch.setattr(sharepoint.graph_client, "get", get)
    versions = run(sharepoint.sp_client.get_item_versions("list", 5))
    assert [v["id"] for v in versions] == ["2.0", "1.0"] and seen[1] == "https://next"


@pytest.mark.parametrize("status, gives_empty", [(404, True), (400, True), (429, False)])
def test_a_missing_item_has_no_versions_but_throttling_still_raises(monkeypatch, status, gives_empty):
    import httpx
    from app.graph import sharepoint

    async def get(url, params=None):
        raise httpx.HTTPStatusError("no", request=None, response=_Response(status))
    monkeypatch.setattr(sharepoint.graph_client, "get", get)
    if gives_empty:
        assert run(sharepoint.sp_client.get_item_versions("list", 5)) == []
    else:
        with pytest.raises(httpx.HTTPStatusError):
            run(sharepoint.sp_client.get_item_versions("list", 5))


def test_a_slow_read_is_tried_again_and_a_dead_one_waits_for_the_next_run(clean, monkeypatch):
    import httpx
    monkeypatch.setattr(step, "READ_BACKOFF_SECONDS", 0)
    _seed(7006, status="pending")                              # field history only
    _seed(7007, status="pending")
    tries = {"7006": 0, "7007": 0}

    async def get_item_versions(list_id, item_id):
        tries[str(item_id)] += 1
        if str(item_id) == "7007" or tries["7006"] < 3:        # 7006 answers on its third try; 7007 never does
            raise httpx.ReadTimeout("slow")
        return [version(1, 0, Status="Pending"), version(3, 5, Status="Pending", Days=2)]
    monkeypatch.setattr(step.sp_client, "get_item_versions", get_item_versions)

    counts = _import()                                         # no exception: the run carries on
    assert counts["unread"] == 1 and counts["remaining"] == 1
    assert _row(7006)[0].extra["sp_history"]["version"] == "3.0"
    assert "sp_history" not in _row(7007)[0].extra             # unstamped, so the next run reads it again
    assert tries == {"7006": 3, "7007": step.READ_ATTEMPTS}


def test_a_refused_read_still_fails_the_run(clean, monkeypatch):
    import httpx
    _seed(7008, status="pending")

    async def get_item_versions(list_id, item_id):
        raise httpx.HTTPStatusError("no", request=None, response=_Response(403))
    monkeypatch.setattr(step.sp_client, "get_item_versions", get_item_versions)
    with pytest.raises(httpx.HTTPStatusError):                 # a permissions change is not hidden
        _import()
