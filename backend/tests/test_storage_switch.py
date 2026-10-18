"""The storage switch: holidays leg, in-flight guard, orchestration, truncation.

Covers the pieces of the SharePoint-to-Postgres switch that ``test_request_sync``
does not:

- the holidays sync step and the three-domain flip (#335): the step copies
  Company Holidays into the ``holidays`` table by ``sp_item_id`` and deletes rows
  SharePoint dropped, the flip sets all three storage settings together, and
  ``flip_back`` points all three at SharePoint again;
- the holidays factory seam honouring the live ``holidays_storage`` setting with
  no fallback to SharePoint once it says Postgres;
- the in-flight guard (``guarded_write`` / ``wait_until_idle``) the switch waits on;
- the background orchestration (``start_switch`` / ``switch_status`` /
  ``SwitchRunning`` / ``AlreadySwitched``);
- the dispatcher skipping request-list webhooks while the switch runs or pauses (#335);
- the sweep due-count ignoring per-item and failed/unfinished sweeps (#336);
- the truncated-read guard (``SharePointReadTruncated``) on both list reads (#336);
- the widened ``Numeric(12, 4)`` columns (#335).

Everything runs on SQLite with a faked ``sp_client``; no test touches real SharePoint.
"""
import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.database import async_session
from app.graph import sharepoint
from app.models.app_setting import AppSetting
from app.models.holiday import Holiday
from app.models.mixins import utcnow
from app.models.sync_run import SyncRun
from app.repositories import factory
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.repositories.sharepoint.holidays import SharePointHolidayRepository
from app.services import storage_switch
from app.services.app_settings import (
    HOLIDAYS_STORAGE,
    POSTGRES,
    REQUESTS_STORAGE,
    SHAREPOINT,
    STAFF_STORAGE,
    get_setting,
    set_setting,
)
from app.services.holiday_import import holiday_sync_step
from app.services.storage_switch import (
    AlreadySwitched,
    SwitchRunning,
    flip_back,
    flip_to_postgres,
    start_switch,
    switch_is_running,
    switch_status,
)
from app.services.switch_pause import (
    SystemPaused,
    begin_pause,
    end_pause,
    guarded,
    guarded_write,
    in_flight_count,
    pinned_requests_backend,
    pinned_staff_backend,
    wait_until_idle,
)

HOLIDAYS = settings.SP_LIST_COMPANY_HOLIDAYS
LEAVE = settings.SP_LIST_LEAVE_REQUESTS


def run(coro):
    """Run one coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


# Tables these tests write, cleared before and after each one.
_TABLES = [Holiday, SyncRun, AppSetting]


async def _wipe():
    """Empty every table this suite writes, so each test starts clean."""
    async with async_session() as session:
        for model in _TABLES:
            await session.execute(delete(model))
        await session.commit()


def _reset_status():
    """Put the in-memory switch status back to idle between tests."""
    storage_switch._SWITCH_STATUS.update(
        state="idle", step=None, started_at=None, finished_at=None, error=None, final_sync=None,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """A clean database and idle switch status, with the post-pause sleep skipped."""
    monkeypatch.setattr(storage_switch, "PAUSE_SETTLE_SECONDS", 0)   # never sleep in a test
    run(_wipe())
    _reset_status()
    yield
    run(_wipe())
    _reset_status()


class FakeSharePoint:
    """An in-memory sp_client: Company Holidays as given, every other list empty."""

    def __init__(self, holidays=None):
        """Hold the holiday list items; all other lists read as empty."""
        self._holidays = holidays or []

    async def get_list_items(self, list_id, filter=None, select=None, expand="fields", top=5000):
        """Every item of a list; only the holidays list has any."""
        if list_id == HOLIDAYS:
            return [copy.deepcopy(i) for i in self._holidays]
        return []

    async def get_list_item_or_none(self, list_id, item_id):
        """No single item is resolvable in these tests."""
        return None


def _holiday(item_id, title, date, province):
    """A Company Holidays list item in the Graph shape."""
    return {"id": str(item_id), "fields": {"Title": title, "Date": date, "Province": province}}


async def _run_holiday_step(fake):
    """Run ``holiday_sync_step`` once in its own committed session, as the sync does.

    Args:
        fake: The FakeSharePoint whose holidays the step reads.

    Returns:
        The step's counts dict.
    """
    async with async_session() as session:
        run_row = SyncRun(job="final_sync")                    # the step signature wants a SyncRun
        session.add(run_row)
        await session.flush()
        counts = await holiday_sync_step(session, run_row)
        await session.commit()
    return counts


async def _holiday_rows():
    """Every holiday row, ordered by its SharePoint id, as (sp_item_id, title)."""
    async with async_session() as session:
        rows = (await session.execute(select(Holiday).order_by(Holiday.sp_item_id))).scalars().all()
    return [(r.sp_item_id, r.title) for r in rows]


# ----- item 12: the holidays sync step -----


def test_holiday_step_copies_rows_by_sp_item_id(monkeypatch):
    # Two SharePoint holidays land as two rows keyed on their SharePoint id.
    fake = FakeSharePoint([
        _holiday(1, "Canada Day", "2026-07-01", "ON"),
        _holiday(2, "BC Day", "2026-08-03", "BC"),
    ])
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    counts = run(_run_holiday_step(fake))
    assert counts == {"seen": 2, "imported": 2, "updated": 0, "deleted": 0}
    assert run(_holiday_rows()) == [("1", "Canada Day"), ("2", "BC Day")]


def test_holiday_step_is_idempotent(monkeypatch):
    # Running the copy twice updates the same rows rather than duplicating them.
    fake = FakeSharePoint([_holiday(1, "Canada Day", "2026-07-01", "ON")])
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    run(_run_holiday_step(fake))
    counts = run(_run_holiday_step(fake))
    assert counts == {"seen": 1, "imported": 0, "updated": 1, "deleted": 0}
    assert run(_holiday_rows()) == [("1", "Canada Day")]        # still one row, not two


def test_holiday_step_deletes_rows_sharepoint_dropped(monkeypatch):
    # A holiday removed in SharePoint is removed from the table on the next run.
    fake = FakeSharePoint([
        _holiday(1, "Canada Day", "2026-07-01", "ON"),
        _holiday(2, "BC Day", "2026-08-03", "BC"),
    ])
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    run(_run_holiday_step(fake))
    fake._holidays = [_holiday(1, "Canada Day", "2026-07-01", "ON")]   # #2 is gone from SharePoint
    counts = run(_run_holiday_step(fake))
    assert counts == {"seen": 1, "imported": 0, "updated": 1, "deleted": 1}
    assert run(_holiday_rows()) == [("1", "Canada Day")]        # the stale row was deleted


# ----- item 12: the flip sets all three settings and copies holidays -----


def test_flip_sets_all_three_settings_and_copies_holidays(monkeypatch):
    fake = FakeSharePoint([_holiday(1, "Canada Day", "2026-07-01", "ON")])
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "sharepoint")
    result = run(flip_to_postgres())
    assert result["storage"] == POSTGRES
    assert result["staff_storage"] is True and result["holidays_storage"] is True
    assert run(get_setting(REQUESTS_STORAGE)) == POSTGRES       # all three flipped together
    assert run(get_setting(STAFF_STORAGE)) == POSTGRES
    assert run(get_setting(HOLIDAYS_STORAGE)) == POSTGRES
    assert run(_holiday_rows()) == [("1", "Canada Day")]        # the final sync copied it


def test_flip_back_points_all_three_at_sharepoint():
    async def body():
        await set_setting(REQUESTS_STORAGE, POSTGRES)
        await set_setting(STAFF_STORAGE, POSTGRES)
        await set_setting(HOLIDAYS_STORAGE, POSTGRES)
        result = await flip_back()
        assert result["storage"] == SHAREPOINT
        assert await get_setting(REQUESTS_STORAGE) == SHAREPOINT
        assert await get_setting(STAFF_STORAGE) == SHAREPOINT
        assert await get_setting(HOLIDAYS_STORAGE) == SHAREPOINT
    run(body())


# ----- item 12 + no fallback: the holidays factory honours the live setting -----


def test_holiday_seam_follows_the_db_setting_with_no_env_fallback(monkeypatch):
    # The env override stays on its default; only the DB setting flips the backend,
    # so a stale env value can never keep holidays on SharePoint after the switch.
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "sharepoint")

    async def before():
        assert await factory.holidays_in_postgres() is False
        return await factory._holiday_backend()

    assert isinstance(run(before()), SharePointHolidayRepository)

    run(set_setting(HOLIDAYS_STORAGE, POSTGRES))               # the switch writes this

    async def after():
        assert await factory.holidays_in_postgres() is True
        return await factory._holiday_backend()

    assert isinstance(run(after()), PostgresHolidayRepository)


def test_holiday_seam_rejects_an_unknown_backend(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "mysql")
    with pytest.raises(NotImplementedError):
        run(factory.holidays_in_postgres())


# ----- item 3: the in-flight guard -----


def test_guarded_write_counts_then_releases():
    async def body():
        assert in_flight_count() == 0
        async with guarded_write():
            assert in_flight_count() == 1                      # counted while the body runs
        assert in_flight_count() == 0                          # and released after
    run(body())


def test_guarded_write_refuses_while_paused_and_leaves_nothing_counted():
    async def body():
        await begin_pause()
        try:
            with pytest.raises(SystemPaused):
                async with guarded_write():
                    pass
            assert in_flight_count() == 0                      # a refused write is not left in flight
        finally:
            await end_pause()
    run(body())


def test_guarded_write_pins_both_backends(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")

    async def body():
        assert pinned_requests_backend() is None and pinned_staff_backend() is None
        async with guarded_write():
            assert pinned_requests_backend() is False          # pinned to SharePoint for the write
            assert pinned_staff_backend() is False
        assert pinned_requests_backend() is None and pinned_staff_backend() is None
    run(body())


def test_a_nested_guarded_write_does_not_refuse_mid_operation():
    # A decision already under way makes a staff write; a pause that began in
    # between must not refuse that inner write and leave the decision half done.
    async def body():
        async with guarded_write():
            await begin_pause()                                # the switch starts mid-decision
            try:
                async with guarded_write():                    # the staff write inside the decision
                    assert in_flight_count() == 2              # both stay counted for the switch to wait on
            finally:
                await end_pause()
        assert in_flight_count() == 0
    run(body())


def test_the_guarded_decorator_refuses_while_paused_and_counts_otherwise():
    seen = []

    @guarded
    async def approve():
        seen.append(in_flight_count())                         # counted while it runs
        return "done"

    async def body():
        assert await approve() == "done"
        await begin_pause()
        try:
            with pytest.raises(SystemPaused):
                await approve()                                # refused, body never runs
        finally:
            await end_pause()
    run(body())
    assert seen == [1]
    assert in_flight_count() == 0


def test_the_flip_deletes_every_webhook_subscription_first(monkeypatch):
    from app.models.webhook_subscription import WebhookSubscription
    from app.tasks import subscription_manager
    fake = FakeSharePoint([])
    monkeypatch.setattr(sharepoint.sp_client, "get_list_items", fake.get_list_items)
    monkeypatch.setattr(sharepoint.sp_client, "get_list_item_or_none", fake.get_list_item_or_none)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "sharepoint")
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "sharepoint")
    deleted = []

    async def fake_delete(subscription_id, list_id):
        # Must run before the settings flip, while SharePoint calls are still allowed.
        assert await get_setting(REQUESTS_STORAGE) != POSTGRES
        deleted.append(subscription_id)
    monkeypatch.setattr(subscription_manager, "delete_subscription", fake_delete)

    async def seed():
        async with async_session() as session:
            await session.execute(delete(WebhookSubscription))
            session.add(WebhookSubscription(id="sub-1", list_id=LEAVE, expiration=utcnow(), client_state="x"))
            await session.commit()

    async def remaining():
        async with async_session() as session:
            return (await session.execute(select(WebhookSubscription))).scalars().all()

    run(seed())
    run(flip_to_postgres())
    assert deleted == ["sub-1"]                                # Graph was asked to drop it
    assert run(remaining()) == []                              # and the row is gone
    assert run(get_setting(REQUESTS_STORAGE)) == POSTGRES


def test_rehearsal_drops_subscription_rows_without_calling_graph(monkeypatch):
    from app.models.webhook_subscription import WebhookSubscription
    from app.services import rehearsal
    from app.tasks import subscription_manager
    monkeypatch.setattr(rehearsal, "sharepoint_writes_allowed", lambda: False)

    async def never(*_a):
        raise AssertionError("rehearsal must not delete a live Graph subscription")
    monkeypatch.setattr(subscription_manager, "delete_subscription", never)

    async def body():
        async with async_session() as session:
            await session.execute(delete(WebhookSubscription))
            session.add(WebhookSubscription(id="sub-2", list_id=LEAVE, expiration=utcnow(), client_state="x"))
            await session.commit()
        return await subscription_manager.delete_all_subscriptions()
    assert run(body()) == 1


def test_wait_until_idle_true_when_idle_and_false_on_timeout():
    async def body():
        assert await wait_until_idle(0.2) is True              # nothing in flight
        async with guarded_write():
            assert await wait_until_idle(0.2) is False         # times out while a write is held
    run(body())


# ----- item 4: the background orchestration -----


def test_start_switch_refuses_a_second_run():
    async def body():
        storage_switch._status_begin()                        # a switch is already running
        assert switch_is_running() is True
        with pytest.raises(SwitchRunning):
            await start_switch()
    run(body())


def test_start_switch_refuses_when_already_in_postgres(monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "postgres")
    monkeypatch.setattr(settings, "STORAGE_EMPLOYEES", "postgres")
    monkeypatch.setattr(settings, "STORAGE_HOLIDAYS", "postgres")
    with pytest.raises(AlreadySwitched):
        run(start_switch())


def test_switch_status_returns_a_copy():
    status = switch_status()
    assert status["state"] == "idle"
    status["state"] = "tampered"                              # mutating the copy
    assert switch_status()["state"] == "idle"                # does not change the real status


# ----- item 7: the dispatcher skips request-list webhooks mid-switch -----


def test_dispatch_skips_a_request_list_while_the_switch_runs(monkeypatch):
    from app.tasks import dispatcher
    seen = []

    async def spy(item_id, fields):
        seen.append(item_id)

    monkeypatch.setattr(dispatcher, "_handle_leave_request_change", spy)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")   # not yet in Postgres
    storage_switch._status_begin()                            # the switch is running
    run(dispatcher.dispatch_change(LEAVE, {"id": "5", "fields": {"Status": "Pending"}}))
    assert seen == []                                         # the change was skipped, not processed


def test_dispatch_skips_a_request_list_while_paused(monkeypatch):
    from app.tasks import dispatcher
    seen = []

    async def spy(item_id, fields):
        seen.append(item_id)

    monkeypatch.setattr(dispatcher, "_handle_leave_request_change", spy)
    monkeypatch.setattr(settings, "STORAGE_REQUESTS", "sharepoint")

    async def body():
        await begin_pause()
        try:
            await dispatcher.dispatch_change(LEAVE, {"id": "5", "fields": {"Status": "Pending"}})
        finally:
            await end_pause()

    run(body())
    assert seen == []                                         # paused: skipped, not processed


# ----- item 8: the sweep due-count ignores per-item and unclean sweeps -----


def _sweep(job, minutes_ago, finished=True, error=None):
    """A SyncRun row for the due-count tests, started ``minutes_ago`` minutes back."""
    at = utcnow() - timedelta(minutes=minutes_ago)
    return SyncRun(job=job, started_at=at, finished_at=(at if finished else None), error=error)


def test_due_count_ignores_per_item_and_unclean_sweeps():
    # Flip 40 min ago, no clean sweep yet, so the first (30 min) check is due. The
    # per-item check and the failed / unfinished sweeps must not count as a check,
    # or the schedule would think the 1-hour check is next and wrongly wait.
    from app.tasks import sync_tasks

    async def body():
        async with async_session() as session:
            session.add(_sweep("final_sync", 40))
            session.add(_sweep("sharepoint_check_item", 5))    # a webhook check: never counts
            session.add(_sweep("sharepoint_check", 10, error="boom"))   # failed: never counts
            session.add(_sweep("sharepoint_check", 12, finished=False))  # unfinished: never counts
            await session.commit()
        assert await sync_tasks._check_is_due() is True
    run(body())


def test_due_count_counts_a_clean_full_sweep():
    # Flip 40 min ago with one clean full sweep already done: the next check is the
    # 1-hour one, which is not due yet, so a clean sweep does postpone it.
    from app.tasks import sync_tasks

    async def body():
        async with async_session() as session:
            session.add(_sweep("final_sync", 40))
            session.add(_sweep("sharepoint_check", 35))        # a clean finished full sweep
            await session.commit()
        assert await sync_tasks._check_is_due() is False
    run(body())


# ----- item 9: a truncated read raises rather than returning short -----


def test_get_list_items_raises_when_truncated(monkeypatch):
    monkeypatch.setattr(sharepoint.sp_client, "site_id", "site")

    async def always_another_page(url, params=None):
        return {"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"}

    monkeypatch.setattr(sharepoint.graph_client, "get", always_another_page)
    with pytest.raises(sharepoint.SharePointReadTruncated):
        run(sharepoint.sp_client.get_list_items("some-list"))


def test_get_delta_raises_when_truncated(monkeypatch):
    monkeypatch.setattr(sharepoint.sp_client, "site_id", "site")

    async def always_another_page(url, params=None):
        return {"value": [{"id": "1"}], "@odata.nextLink": "https://graph/next"}

    monkeypatch.setattr(sharepoint.graph_client, "get", always_another_page)
    with pytest.raises(sharepoint.SharePointReadTruncated):
        run(sharepoint.sp_client.get_delta("some-list"))


# ----- item 1: the widened numeric columns -----


def test_balance_numeric_columns_are_widened_to_12_4():
    # Four decimals are needed because overtime enters in hours and is divided by 8,
    # so a balance can carry up to 0.0001 precision; the column must not round it.
    from app.models.balance_ledger import BalanceLedger
    from app.models.request_record import RequestRecord
    from app.models.staff import Staff

    columns = [
        RequestRecord.__table__.c.days,
        RequestRecord.__table__.c.hours,
        RequestRecord.__table__.c.partial_hours,
        BalanceLedger.__table__.c.before,
        BalanceLedger.__table__.c.after,
        BalanceLedger.__table__.c.change,
        Staff.__table__.c.vacation_balance,
        Staff.__table__.c.sick_balance,
        Staff.__table__.c.overtime_balance,
        Staff.__table__.c.carry_over,
        Staff.__table__.c.payout,
    ]
    for column in columns:
        assert column.type.precision == 12, f"{column} precision"
        assert column.type.scale == 4, f"{column} scale"
