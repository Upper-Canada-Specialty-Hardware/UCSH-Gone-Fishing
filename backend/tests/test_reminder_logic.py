"""Deterministic logic tests for non-expiring links + reminder follow-ups.

Covers the decision logic that can't be observed quickly in production (the
30/7-day cadence, the cutoff rules, the force-bump version math, and the exp=0
no-expiry sentinel). The actual email send + supersession is verified live.
"""

import asyncio
import contextlib
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.time_utils import utcnow_naive
from app.services.approval_links import (
    generate_approval_url,
    validate_approval_token,
    _sign,
    NO_EXPIRY,
)
from app.services.approval_versions import (
    bump_and_snapshot,
    get_current_version,
    MATERIAL_FIELDS_LEAVE,
)
from app.tasks import reminders


# ----- non-expiring approval links -----

def test_no_expiry_sentinel_never_expires():
    token = _sign("leave", "123", "approve", "45", "0", 1)
    ok, msg = validate_approval_token("leave", "123", "approve", "45", token, "0", 1)
    assert ok, msg


def test_past_real_expiry_still_rejected():
    # exp=100 is 1970 - a pre-existing 72h link should still lapse.
    token = _sign("leave", "123", "approve", "45", "100", 1)
    ok, msg = validate_approval_token("leave", "123", "approve", "45", token, "100", 1)
    assert not ok and "expired" in msg.lower()


def test_future_real_expiry_valid():
    future = str(int(utcnow_naive().timestamp()) + 3600)
    token = _sign("leave", "123", "approve", "45", future, 1)
    ok, _ = validate_approval_token("leave", "123", "approve", "45", token, future, 1)
    assert ok


def test_tampered_token_rejected():
    ok, msg = validate_approval_token("leave", "123", "approve", "45", "deadbeef", "0", 1)
    assert not ok and "token" in msg.lower()


def test_generate_url_defaults_to_no_expiry():
    url = generate_approval_url("leave", 123, "approve", 45)
    assert f"exp={NO_EXPIRY}" in url


def test_generate_url_with_hours_sets_real_future_expiry():
    url = generate_approval_url("leave", 123, "approve", 45, expiry_hours=72)
    exp = int(url.split("exp=")[1].split("&")[0])
    assert exp > int(utcnow_naive().timestamp())


# ----- version / force-bump math -----

def test_bump_and_snapshot_flow():
    asyncio.run(_bump_flow())


async def _bump_flow():
    from app.database import engine, Base, async_session
    from app.models import RequestApprovalState

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    lid = "L"
    iid = f"t-{uuid.uuid4().hex}"
    snap = {"Days": 1, "LeaveType": "Vacation", "StartDate": "2026-07-01", "EndDate": "2026-07-02"}

    # first send -> v1
    assert await bump_and_snapshot(lid, iid, snap, MATERIAL_FIELDS_LEAVE) == 1

    # benign re-send (no change, no force) -> version stays put
    assert await bump_and_snapshot(lid, iid, snap, MATERIAL_FIELDS_LEAVE) == 1

    # forced reminder bump (no change) -> version++ and reminder_count++
    assert await bump_and_snapshot(lid, iid, snap, MATERIAL_FIELDS_LEAVE, force_bump=True) == 2
    async with async_session() as s:
        row = await s.get(RequestApprovalState, (lid, iid))
        assert row.reminder_count == 1
        assert not row.reminders_closed

    assert await bump_and_snapshot(lid, iid, snap, MATERIAL_FIELDS_LEAVE, force_bump=True) == 3
    async with async_session() as s:
        row = await s.get(RequestApprovalState, (lid, iid))
        assert row.reminder_count == 2

    # material change (admin edit) -> version++ and reminder cadence restarts
    assert await bump_and_snapshot(lid, iid, dict(snap, Days=2), MATERIAL_FIELDS_LEAVE) == 4
    async with async_session() as s:
        row = await s.get(RequestApprovalState, (lid, iid))
        assert row.reminder_count == 0
        assert not row.reminders_closed

    assert await get_current_version(lid, iid) == 4


# ----- reminder cadence -----

def _row(count, days_ago):
    return SimpleNamespace(
        reminder_count=count,
        last_emailed_at=utcnow_naive() - timedelta(days=days_ago),
    )


def test_first_reminder_due_at_30_days():
    now = utcnow_naive()
    assert reminders._is_due(_row(0, 31), now) is True
    assert reminders._is_due(_row(0, 29), now) is False


def test_repeat_reminder_due_at_7_days():
    now = utcnow_naive()
    assert reminders._is_due(_row(1, 8), now) is True
    assert reminders._is_due(_row(1, 6), now) is False


# ----- cutoff rules -----

def test_leave_cutoff_on_start_date():
    assert reminders._cutoff_passed("leave", {"StartDate": "2020-01-01"}, {}, 0) is True
    assert reminders._cutoff_passed("leave", {"StartDate": "2999-01-01"}, {}, 0) is False


def test_overtime_cutoff_on_start_date():
    assert reminders._cutoff_passed("overtime", {"StartDate": "2020-01-01"}, {}, 0) is True


def test_carryover_cutoff_count_cap():
    assert reminders._cutoff_passed("carryover-payout", {}, {}, reminders.MAX_REMINDERS_WITHOUT_DATE) is True
    assert reminders._cutoff_passed("carryover-payout", {}, {}, 0) is False


def test_carryover_cutoff_year_end():
    item = {"createdDateTime": "2000-01-15T00:00:00Z"}
    assert reminders._cutoff_passed("carryover-payout", {}, item, 0) is True


# ----- already-actioned detection -----

def test_is_processed_predicates():
    assert reminders._is_processed({"Status": "Approved"}, "leave") is True
    assert reminders._is_processed({"Status": "Pending", "ApproveProcessedFlag": "Processed"}, "leave") is True
    assert reminders._is_processed({"Status": "Pending", "ApproveProcessedFlag": "Not Processed"}, "leave") is False
    assert reminders._is_processed({"Status": "Pending"}, "overtime") is False
    assert reminders._is_processed({"Status": "Pending", "SystemState": "Processed"}, "carryover-payout") is True


# ----- deleted SharePoint items -----

def _http_error(status_code):
    request = httpx.Request("GET", "https://graph.microsoft.com/v1.0/items/1")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@contextlib.contextmanager
def _get_list_item_raises(status_code):
    """Patch the shared sp_client singleton, then fully undo it.

    Restoring by re-assignment would leave a bound method in the instance
    __dict__ for the rest of the run, shadowing the class method so any later
    class-level patch of get_list_item silently does nothing. Delete instead.
    """
    async def _raise(list_id, item_id):
        raise _http_error(status_code)

    client = reminders.sp_client
    had_own = "get_list_item" in client.__dict__
    previous = client.__dict__.get("get_list_item")
    client.get_list_item = _raise
    try:
        yield
    finally:
        if had_own:
            client.get_list_item = previous
        else:
            del client.get_list_item


@pytest.mark.parametrize(
    "status_code, expect_closed",
    [
        # 404 = deleted in SharePoint. Nothing left to remind about, and leaving
        # the row open makes every hourly scan retry it and log a traceback.
        (404, True),
        # 503 is not proof the request is gone - it must stay open and retry.
        (503, False),
    ],
)
def test_reminders_close_only_when_the_item_is_really_gone(status_code, expect_closed):
    asyncio.run(_missing_item_flow(status_code, expect_closed))


async def _missing_item_flow(status_code, expect_closed):
    from app.database import engine, Base, async_session
    from app.models import RequestApprovalState

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    lid = settings.SP_LIST_LEAVE_REQUESTS
    iid = f"missing-{status_code}-{uuid.uuid4().hex}"
    await bump_and_snapshot(lid, iid, {"Days": 1}, MATERIAL_FIELDS_LEAVE)

    async with async_session() as s:
        row = await s.get(RequestApprovalState, (lid, iid))

    with _get_list_item_raises(status_code):
        if expect_closed:
            await reminders._process_row(row)
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await reminders._process_row(row)

    async with async_session() as s:
        row = await s.get(RequestApprovalState, (lid, iid))
        assert row.reminders_closed is expect_closed


def test_admin_reminder_on_deleted_item_returns_error_not_500():
    """send_reminder_now is the sibling call site of _process_row.

    A deleted item must come back as a dict the dashboard can turn into a 400,
    not an httpx exception escaping into an unhandled 500.
    """
    with _get_list_item_raises(404):
        result = asyncio.run(reminders.send_reminder_now("leave", "3402"))
    assert "error" in result


def test_admin_reminder_propagates_transient_errors():
    with _get_list_item_raises(503):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(reminders.send_reminder_now("leave", "3402"))
