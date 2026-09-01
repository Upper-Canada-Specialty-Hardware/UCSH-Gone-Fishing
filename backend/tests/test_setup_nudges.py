"""Nudging the creator of a Staff Directory record whose setup would stall a request.

The sweep behind this already knew which records were broken; what it could not
do was tell anybody. The risk in telling people is the opposite of the risk in
staying silent, so most of what is covered here is restraint: exactly one
recipient (the creator, never the shared mailbox the last-editor field points
at), one email a week while nothing changes, and nothing at all once the record
is fixed.
"""

import asyncio
import importlib.util
import os
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.config import settings
from app.database import Base
from app.models import StaffSetupNudge
from app.services import employee_validation as ev
from app.tasks import setup_nudges as task
from app.templates_render import render_staff_setup_issues

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


# ----- deciding whether to send -----

class _Row:
    """Stands in for a StaffSetupNudge row; decide() only reads two fields."""

    def __init__(self, signature, last_sent_at):
        self.issue_signature = signature
        self.last_sent_at = last_sent_at


def test_a_record_never_nudged_gets_a_first_email():
    assert task.decide(None, "supervisor_set", NOW) == "send_first"


def test_a_new_problem_is_not_made_to_wait_for_the_weekly_slot():
    yesterday = _Row("supervisor_set", NOW - timedelta(days=1))

    assert task.decide(yesterday, "location_province,supervisor_set", NOW) == "send_changed"


def test_the_same_problem_is_repeated_after_a_week():
    a_week_ago = _Row("supervisor_set", NOW - timedelta(days=7))

    assert task.decide(a_week_ago, "supervisor_set", NOW) == "send_weekly"


def test_the_same_problem_is_not_repeated_after_six_days():
    six_days_ago = _Row("supervisor_set", NOW - timedelta(days=6))

    assert task.decide(six_days_ago, "supervisor_set", NOW) == "skip"


def test_the_same_problem_the_same_day_is_skipped():
    this_morning = _Row("supervisor_set", NOW - timedelta(hours=3))

    assert task.decide(this_morning, "supervisor_set", NOW) == "skip"


def test_a_naive_timestamp_is_read_as_utc():
    # SQLite hands back naive datetimes even from a timezone=True column, and a
    # comparison against an aware `now` would otherwise raise.
    naive = _Row("supervisor_set", (NOW - timedelta(days=8)).replace(tzinfo=None))

    assert task.decide(naive, "supervisor_set", NOW) == "send_weekly"


# ----- who the sweep can address -----

def _setup_row(record):
    """Grade one record with every lookup empty: only the item's own fields matter."""
    return ev.build_setup_row(
        record=record,
        staff_by_name={},
        name_counts={},
        directory={},
        sp_user_to_name={},
        holidays_by_province={},
        sample_start=date(2026, 9, 7),
        sample_end=date(2026, 9, 8),
        today=date(2026, 8, 31),
    )


def test_a_record_created_by_a_person_carries_their_email_and_a_link():
    row = _setup_row({
        "id": "41",
        "webUrl": "https://ucshca.sharepoint.com/sites/UCSHBulletinBoard/Lists/Staff/41",
        "createdBy": {"user": {"displayName": "HR Person", "email": "HR.Person@ucsh.ca"}},
        "fields": {"Title": "Alice Worker"},
    })

    assert row["creator_email"] == "hr.person@ucsh.ca"   # lowercased for comparison
    assert row["record_url"].endswith("/41")


def test_a_record_the_app_created_itself_has_no_creator_to_nudge():
    # createdBy carries an application identity and no user at all - there is
    # nobody behind it, which is why the sweep skips rather than substitutes.
    row = _setup_row({
        "id": "42",
        "createdBy": {"application": {"displayName": "UCSH Gone Fishing"}},
        "fields": {"Title": "Bob Worker"},
    })

    assert row["creator_email"] is None


def test_an_item_with_no_created_by_or_url_reports_neither():
    row = _setup_row({"id": "43", "fields": {"Title": "Carol Worker"}})

    assert row["creator_email"] is None
    assert row["record_url"] is None


# ----- the sweep -----

async def _reset():
    """Start each test from an empty nudge table and an unclaimed day."""
    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.models import ProcessingLog

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(StaffSetupNudge))
        await session.execute(
            delete(ProcessingLog).where(ProcessingLog.action == task.SWEEP_ACTION)
        )
        await session.commit()


async def _rows():
    from app.database import async_session
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(select(StaffSetupNudge))
        return {row.employee_id: row for row in result.scalars().all()}


def _flagged(
    employee_id="1", name="Alice Worker", codes=("supervisor_set",),
    creator="creator@ucsh.ca", record_url=None,
):
    return {
        "employee_id": str(employee_id),
        "employee_name": name,
        "department": "Warehouse",
        "location": "Barrie",
        "fails": [
            {"code": code, "category": "supervisor", "detail": f"{code} is wrong"}
            for code in codes
        ],
        "warns": [],
        "creator_email": creator,
        "record_url": record_url,
    }


def _patch(monkeypatch, flagged, sent, total_checked=10, fails_for=None):
    """Point the task at a fixed sweep result and capture what it would send."""
    async def _validate():
        return {
            "total_checked": total_checked,
            "flagged": flagged,
            "directory_unreadable": False,
        }

    async def _send(**kwargs):
        if fails_for and kwargs["to"] == [fails_for]:
            raise RuntimeError("mailbox rejected the message")
        sent.append(kwargs)

    monkeypatch.setattr(task, "validate_all_employee_setups", _validate)
    monkeypatch.setattr(task, "send_email", _send)


def _addresses(sent):
    """Every address the sweep touched, however it was passed."""
    seen = set()
    for message in sent:
        seen.update(message["to"])
        seen.update(message.get("cc") or [])
    return seen


def test_only_the_creator_is_emailed(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged(creator="creator@ucsh.ca")], sent)
        return await task.run_setup_nudge_sweep(NOW)

    summary = asyncio.run(flow())

    assert summary["sent"] == 1
    assert len(sent) == 1
    assert sent[0]["to"] == ["creator@ucsh.ca"]
    # Nothing else is ever addressed - in particular not the last editor, which
    # in this tenant is a shared HR mailbox that would receive every nudge.
    assert _addresses(sent) == {"creator@ucsh.ca"}
    assert "Alice Worker" in sent[0]["subject"]


def test_the_email_says_what_is_wrong_with_that_record(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged(codes=("supervisor_set",))], sent)
        await task.run_setup_nudge_sweep(NOW)

    asyncio.run(flow())

    body = sent[0]["html_body"]
    assert "No supervisor assigned" in body          # the plain-language title
    assert "supervisor_set is wrong" in body         # the check's own detail


def test_a_record_with_no_creator_is_counted_and_left_alone(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged(creator=None)], sent)
        summary = await task.run_setup_nudge_sweep(NOW)
        return summary, await _rows()

    summary, rows = asyncio.run(flow())

    assert sent == []
    assert summary["skipped_unresolvable"] == 1
    assert summary["sent"] == 0
    # No row either: nothing was said, so nothing is remembered.
    assert rows == {}


def test_the_same_problem_is_not_re_sent_the_same_day(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged()], sent)
        first = await task.run_setup_nudge_sweep(NOW)
        second = await task.run_setup_nudge_sweep(NOW + timedelta(hours=2))
        return first, second

    first, second = asyncio.run(flow())

    assert (first["sent"], second["sent"]) == (1, 0)
    assert second["skipped_recent"] == 1
    assert len(sent) == 1


def test_a_record_broken_in_a_new_way_is_told_again_at_once(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged(codes=("supervisor_set",))], sent)
        await task.run_setup_nudge_sweep(NOW)

        _patch(monkeypatch, [_flagged(codes=("supervisor_set", "location_province"))], sent)
        return await task.run_setup_nudge_sweep(NOW + timedelta(hours=2)), await _rows()

    summary, rows = asyncio.run(flow())

    assert summary["sent"] == 1
    assert len(sent) == 2
    assert rows["1"].send_count == 2
    assert rows["1"].issue_signature == "location_province,supervisor_set"


def test_a_re_nudge_keeps_the_date_the_record_first_broke(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged()], sent)
        await task.run_setup_nudge_sweep(NOW)
        await task.run_setup_nudge_sweep(NOW + timedelta(days=8))
        return await _rows()

    rows = asyncio.run(flow())

    assert len(sent) == 2
    assert rows["1"].send_count == 2
    assert rows["1"].last_sent_at > rows["1"].first_sent_at


def test_a_fixed_record_is_forgotten_rather_than_left_on_the_clock(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged()], sent)
        await task.run_setup_nudge_sweep(NOW)

        _patch(monkeypatch, [], sent)   # nothing flagged any more
        return await task.run_setup_nudge_sweep(NOW + timedelta(days=1)), await _rows()

    summary, rows = asyncio.run(flow())

    assert summary["resolved"] == 1
    assert rows == {}


def test_a_record_that_breaks_again_starts_a_fresh_first_email(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(monkeypatch, [_flagged()], sent)
        await task.run_setup_nudge_sweep(NOW)

        _patch(monkeypatch, [], sent)
        await task.run_setup_nudge_sweep(NOW + timedelta(days=1))

        # Broken again the next day, well inside the weekly window.
        _patch(monkeypatch, [_flagged()], sent)
        return await task.run_setup_nudge_sweep(NOW + timedelta(days=2)), await _rows()

    summary, rows = asyncio.run(flow())

    assert summary["sent"] == 1
    assert len(sent) == 2
    assert rows["1"].send_count == 1


def test_one_failed_send_does_not_stop_the_rest(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(
            monkeypatch,
            [
                _flagged(1, "Alice Worker", creator="broken@ucsh.ca"),
                _flagged(2, "Bob Worker", creator="fine@ucsh.ca"),
            ],
            sent,
            fails_for="broken@ucsh.ca",
        )
        return await task.run_setup_nudge_sweep(NOW), await _rows()

    summary, rows = asyncio.run(flow())

    assert summary["sent"] == 1
    assert _addresses(sent) == {"fine@ucsh.ca"}
    # The failed one keeps no row, so tomorrow's sweep tries it again.
    assert set(rows) == {"2"}


def test_the_summary_reports_every_count(monkeypatch):
    sent = []

    async def flow():
        await _reset()
        _patch(
            monkeypatch,
            [_flagged(1), _flagged(2, "Bob Worker", creator=None)],
            sent,
            total_checked=42,
        )
        return await task.run_setup_nudge_sweep(NOW)

    summary = asyncio.run(flow())

    assert summary == {
        "checked": 42,
        "flagged": 2,
        "sent": 1,
        "skipped_unresolvable": 1,
        "skipped_recent": 0,
        "resolved": 0,
    }


# ----- once a day, whatever restarts -----

def test_the_sweep_runs_once_per_toronto_day(monkeypatch):
    runs = []

    async def flow():
        await _reset()

        async def _sweep(now=None):
            runs.append(now)
            return {}

        monkeypatch.setattr(task, "run_setup_nudge_sweep", _sweep)
        morning = datetime(2026, 8, 31, 7, 30, tzinfo=task.TORONTO_TZ)
        first = await task._run_for_today(morning)
        second = await task._run_for_today(morning + timedelta(hours=4))
        return first, second

    first, second = asyncio.run(flow())

    assert (first, second) == (True, False)
    assert len(runs) == 1


def test_nothing_runs_before_the_sweep_hour(monkeypatch):
    runs = []

    async def flow():
        await _reset()

        async def _sweep(now=None):
            runs.append(now)
            return {}

        monkeypatch.setattr(task, "run_setup_nudge_sweep", _sweep)
        return await task._run_for_today(datetime(2026, 8, 31, 6, 59, tzinfo=task.TORONTO_TZ))

    assert asyncio.run(flow()) is False
    assert runs == []


def test_a_new_day_runs_again(monkeypatch):
    runs = []

    async def flow():
        await _reset()

        async def _sweep(now=None):
            runs.append(now)
            return {}

        monkeypatch.setattr(task, "run_setup_nudge_sweep", _sweep)
        await task._run_for_today(datetime(2026, 8, 31, 8, 0, tzinfo=task.TORONTO_TZ))
        return await task._run_for_today(datetime(2026, 9, 1, 8, 0, tzinfo=task.TORONTO_TZ))

    assert asyncio.run(flow()) is True
    assert len(runs) == 2


# ----- the email itself -----

ISSUES = [
    {
        "code": "supervisor_set",
        "title": "No supervisor assigned",
        "detail": "No supervisor is set on the Staff Directory record.",
        "fix": "Set their supervisor in the Staff Directory.",
    },
    {
        "code": "location_province",
        "title": "Office location not recognized",
        "detail": "Unknown location: Mars.",
        "fix": "Choose a valid office location.",
    },
]

LEAD = (
    "You created a record that does not have the correct setup as per Gone "
    "Fishing's requirements. Please fix the issues below accordingly."
)


def test_the_email_opens_on_the_agreed_sentence_and_names_the_record():
    html = render_staff_setup_issues("Alice Worker", ISSUES)

    assert LEAD in html
    assert "Alice Worker" in html


def test_every_issue_is_shown_with_its_detail_and_its_fix():
    html = render_staff_setup_issues("Alice Worker", ISSUES)

    for issue in ISSUES:
        assert issue["title"] in html
        assert issue["detail"] in html
        assert issue["fix"] in html


def test_the_record_link_appears_only_when_there_is_one():
    with_link = render_staff_setup_issues("Alice Worker", ISSUES, "https://sp/items/41")
    without = render_staff_setup_issues("Alice Worker", ISSUES)

    assert "https://sp/items/41" in with_link
    assert "href" not in without


def test_the_email_carries_no_em_dash():
    html = render_staff_setup_issues("Alice Worker", ISSUES, "https://sp/items/41")

    assert chr(0x2014) not in html   # an em dash anywhere would be a house-style bug


def test_every_problem_code_the_validator_can_report_has_wording():
    # A fail with no wording still reaches the reader, but as a bare code. The
    # two files are kept in step by hand, so this is the thing that says when
    # they have drifted.
    from app.services.setup_problem_copy import PROBLEM_INFO

    row = _flagged(codes=("supervisor_set", "not_a_real_code"))
    issues = task.issues_for(row)

    assert issues[0]["title"] == PROBLEM_INFO["supervisor_set"]["title"]
    assert issues[1]["title"] == "not_a_real_code"
    assert issues[1]["fix"] == ""


# ----- the table and its migration -----

def test_the_nudge_table_is_registered_and_builds_on_sqlite():
    assert "staff_setup_nudge" in Base.metadata.tables

    columns = set(StaffSetupNudge.__table__.columns.keys())
    assert columns == {
        "employee_id", "issue_signature", "recipient",
        "first_sent_at", "last_sent_at", "send_count",
    }

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)


def _migration(filename):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "alembic", "versions", filename,
    )
    spec = importlib.util.spec_from_file_location(filename, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_follows_the_previous_one():
    module = _migration("0007_staff_setup_nudge.py")

    assert module.revision == "0007"
    assert module.down_revision == "0006"


# ----- the admin endpoint -----

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    from app.routes.dashboard import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    return TestClient(app, raise_server_exceptions=False)


SUMMARY = {
    "checked": 42, "flagged": 2, "sent": 1,
    "skipped_unresolvable": 1, "skipped_recent": 0, "resolved": 0,
}


def test_the_endpoint_returns_the_summary(client, monkeypatch):
    async def _sweep():
        return SUMMARY

    monkeypatch.setattr(task, "run_setup_nudge_sweep", _sweep)

    resp = client.post("/admin/employee-setup/nudge")

    assert resp.status_code == 200          # no token supplied, still accepted
    assert resp.json() == SUMMARY


def test_the_endpoint_sends_nothing_in_reporting_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    called = []

    async def _sweep():
        called.append(True)
        return SUMMARY

    monkeypatch.setattr(task, "run_setup_nudge_sweep", _sweep)

    resp = client.post("/admin/employee-setup/nudge")

    assert resp.status_code == 503
    assert called == []
