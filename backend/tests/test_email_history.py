"""The 30 days before the send log existed can still be read back.

Every email the server sends is written next to a record that survives: the
approval-state row, the processing claim, the renewal claim, the nudge row.
These cover turning those records back into a dated timeline for one person,
and the piecewise degradation when a source cannot be read.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import delete

from app.config import settings
from app.database import Base, async_session, engine
from app.models import (
    DashboardLinkState,
    ProcessingLog,
    RequestApprovalState,
    StaffSetupNudge,
)
from app.models.mixins import utcnow
from app.services import email_history as history
from app.tasks.dashboard_links import CLAIM_NAMESPACE

LEAVE = settings.SP_LIST_LEAVE_REQUESTS
OVERTIME = settings.SP_LIST_OVERTIME_REQUESTS
CARRYOVER = settings.SP_LIST_CARRYOVER_PAYOUT
LISTS = [LEAVE, OVERTIME, CARRYOVER]

NOW = utcnow()


def _naive(days: float):
    """A past instant as the naive UTC value the older tables store."""
    return (NOW - timedelta(days=days)).replace(tzinfo=None)


def _sp(days: float) -> str:
    """A past instant as SharePoint writes it into Created."""
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _reset_and_seed():
    """Known rows in every table the reconstruction reads."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await session.execute(delete(RequestApprovalState).where(RequestApprovalState.list_id.in_(LISTS)))
        await session.execute(delete(ProcessingLog).where(ProcessingLog.list_id.in_(LISTS + [CLAIM_NAMESPACE])))
        await session.execute(delete(DashboardLinkState))
        await session.execute(delete(StaffSetupNudge))

        # Approval emails were built for these requests (one row per request).
        session.add_all([
            RequestApprovalState(list_id=LEAVE, item_id="3001", current_snapshot={}, last_emailed_at=_naive(2)),
            RequestApprovalState(list_id=LEAVE, item_id="3002", current_snapshot={}, last_emailed_at=_naive(1), reminder_count=2),
            RequestApprovalState(list_id=LEAVE, item_id="3003", current_snapshot={}, last_emailed_at=_naive(60)),
            RequestApprovalState(list_id=CARRYOVER, item_id="5001", current_snapshot={}, last_emailed_at=_naive(1)),
        ])
        # Decisions and renewals each claimed a processing row when they sent.
        session.add_all([
            ProcessingLog(list_id=LEAVE, item_id="3001", action="approve", processed_at=_naive(1)),
            ProcessingLog(list_id=LEAVE, item_id="3002", action="reject", processed_at=_naive(0.5)),
            ProcessingLog(list_id=CLAIM_NAMESPACE, item_id="412", action=_sp(5)[:10], processed_at=_naive(5)),
            ProcessingLog(list_id=CLAIM_NAMESPACE, item_id="999", action=_sp(4)[:10], processed_at=_naive(4)),
        ])
        session.add(DashboardLinkState(employee_id="412", last_sent_at=NOW - timedelta(days=1), expires_at=NOW + timedelta(days=29)))
        session.add(StaffSetupNudge(
            employee_id="9", issue_signature="no_manager", recipient="Worker@ucsh.com ",
            first_sent_at=NOW - timedelta(days=10), last_sent_at=NOW - timedelta(days=3),
        ))
        await session.commit()


def _install_sharepoint(monkeypatch):
    """Canned Staff Directory context and request lists.

    Test Worker (id 412) submitted leave #3001 and payout #5001, and is listed
    as a manager of Other Person, who submitted leave #3002 (in window) and
    #3003 (60 days ago).
    """
    async def _ctx(employee_name, notes):
        if not employee_name:
            return {}, {}, set()      # mirrors the real helper: no name, no directory read
        sp_user_to_name = {7: "Test Worker", 8: "Other Person"}
        staff_by_id = {412: {"fields": {"Title": "Test Worker"}}, 9: {"fields": {"Title": "Broken Record"}}}
        return sp_user_to_name, staff_by_id, {"other person"}

    async def _items(list_id, **kwargs):
        if list_id == LEAVE:
            return [
                {"id": "3001", "fields": {"SubmittedTestLookupId": 7, "Created": _sp(2), "Status": "Approved"}},
                {"id": "3002", "fields": {"SubmittedTestLookupId": 8, "Created": _sp(3), "Status": "Rejected"}},
                {"id": "3003", "fields": {"SubmittedTestLookupId": 8, "Created": _sp(60), "Status": "Pending"}},
            ]
        if list_id == CARRYOVER:
            return [{"id": "5001", "fields": {"EmployeeID": 412, "TypeofRequest": "Payout", "Created": _sp(1)}}]
        return []

    monkeypatch.setattr(history, "_staff_context", _ctx)
    monkeypatch.setattr(history.sp_client, "get_list_items", _items)


def _run(**kwargs):
    async def flow():
        await _reset_and_seed()
        return await history.reconstruct_email_history(**kwargs)

    return asyncio.run(flow())


def test_timeline_rebuilds_every_kind_of_send_for_one_person(monkeypatch):
    _install_sharepoint(monkeypatch)
    result = _run(employee_id="412", employee_name="Test Worker", address="worker@ucsh.com",
                  since=NOW - timedelta(days=30))
    by_subject = {}
    for e in result["events"]:
        by_subject.setdefault(e["subject"], []).append(e)

    # As submitter: the intake confirmation, dated by the item's creation.
    (intake,) = by_subject["Leave Request Received - Test Worker"]
    assert intake["date_precision"] == "approximate"
    assert intake["request_id"] == "3001"
    assert intake["to"] == ["worker@ucsh.com"]
    (payout,) = by_subject["Request Received for Payout"]
    assert payout["request_type"] == "carryover-payout"

    # As submitter: the decision, dated exactly by the processing claim.
    (approved,) = by_subject["Test Worker - Leave Request: Approved"]
    assert approved["date_precision"] == "exact"
    (balance,) = by_subject["Updated Leave Balance - Test Worker"]
    assert balance["also_to"] == "the approving manager"

    # As manager of Other Person: the approval request, latest date only, with
    # the reminder count surfaced. The rejection email went to the employee
    # alone, so it does not appear here.
    (asked,) = by_subject["Reminder: Leave Request - Other Person"]
    assert asked["date_precision"] == "latest_only"
    assert "2 earlier" in asked["note"]
    assert "Other Person - Leave Request: Rejected" not in by_subject

    # Renewal claim for this person only; nudge from first and last send.
    (renewal,) = by_subject["Your Dashboard Link"]
    assert renewal["source"] == "processing_log dashboard-link-renewal"
    nudges = by_subject["Staff Directory record needs attention - Broken Record"]
    assert {n["source"] for n in nudges} == {"staff_setup_nudge.last_sent_at", "staff_setup_nudge.first_sent_at"}

    # Request #3003 was emailed 60 days ago: outside the window.
    assert all(e["request_id"] != "3003" for e in result["events"])

    # Newest first, and the summary field for the most recent dashboard-link email.
    dates = [e["date"] for e in result["events"]]
    assert dates == sorted(dates, reverse=True)
    assert result["latest_dashboard_link_email_at"] is not None
    assert any("not listed" in n for n in result["notes"])


def test_without_a_directory_match_the_id_based_sources_still_answer(monkeypatch):
    _install_sharepoint(monkeypatch)
    # Directory lookup failed upstream: no name, but the id and address are known.
    result = _run(employee_id="412", employee_name=None, address="worker@ucsh.com",
                  since=NOW - timedelta(days=30))
    subjects = {e["subject"] for e in result["events"]}

    assert "Request Received for Payout" in subjects          # carryover matches by EmployeeID
    assert "Your Dashboard Link" in subjects                   # renewal claim matches by id
    assert "Staff Directory record needs attention - record #9" in subjects  # nudge matches by address
    assert "Leave Request Received - Test Worker" not in subjects  # needs the name


def test_a_list_that_cannot_be_read_is_reported_not_fatal(monkeypatch):
    _install_sharepoint(monkeypatch)

    async def _broken(list_id, **kwargs):
        if list_id == LEAVE:
            raise RuntimeError("Graph 503")
        return []

    monkeypatch.setattr(history.sp_client, "get_list_items", _broken)
    result = _run(employee_id="412", employee_name="Test Worker", address="worker@ucsh.com",
                  since=NOW - timedelta(days=30))

    assert any("leave requests could not be read" in n for n in result["notes"])
    assert {e["subject"] for e in result["events"]} >= {"Your Dashboard Link"}
