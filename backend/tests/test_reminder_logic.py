"""Deterministic logic tests for non-expiring links + reminder follow-ups.

Covers the decision logic that can't be observed quickly in production (the
30/7-day cadence, the cutoff rules, the force-bump version math, and the exp=0
no-expiry sentinel). The actual email send + supersession is verified live.
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

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
    # exp=100 is 1970 — a pre-existing 72h link should still lapse.
    token = _sign("leave", "123", "approve", "45", "100", 1)
    ok, msg = validate_approval_token("leave", "123", "approve", "45", token, "100", 1)
    assert not ok and "expired" in msg.lower()


def test_future_real_expiry_valid():
    future = str(int(datetime.utcnow().timestamp()) + 3600)
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
    assert exp > int(datetime.utcnow().timestamp())


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
        last_emailed_at=datetime.utcnow() - timedelta(days=days_ago),
    )


def test_first_reminder_due_at_30_days():
    now = datetime.utcnow()
    assert reminders._is_due(_row(0, 31), now) is True
    assert reminders._is_due(_row(0, 29), now) is False


def test_repeat_reminder_due_at_7_days():
    now = datetime.utcnow()
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
