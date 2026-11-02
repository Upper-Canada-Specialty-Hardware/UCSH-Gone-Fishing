"""A manager makes a request for someone on their team, approved as made (#523).

The routes in ``routes/team_manage.py`` reuse the admin New request's pieces
(the form check, the create, the approval, the fallback), so these tests stub
those pieces and check what the team routes add on top: who may be the target,
the "Made by" note, the carry-over dates, the approval under the manager's own
id and channel, and the fallback to an ordinary pending request. The old
BC-only "Log sick day" route and its flag are gone; the last test checks that.
"""
import asyncio
from datetime import date

import pytest
from fastapi import HTTPException

from app.config import settings
from app.main import app
from app.routes import team_manage as tm
from app.routes.dashboard import DashboardUser
from app.services.request_intake import RequestTypeClosed

STEVE = DashboardUser(role="manager", user_id="10")    # the signed-in manager
TEAMMATE = {"id": "20", "fields": {"Title": "Omar Ali", "EmailAddress": "omar@ucsh.com"}}


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


@pytest.fixture
def stubbed(monkeypatch):
    """Stub everything the team request routes call, and record what they got.

    Returns:
        A dict the stubs fill: "submitted" (type, form, employee, source,
        notify), "approved" (type, item_id, approver_id, channel), "recovered"
        (item ids); plus "approve_result" and "recover_result" the test may set.
    """
    seen = {"submitted": [], "approved": [], "recovered": [], "approve_result": {"status": "approved"}, "recover_result": True}

    async def signed_in_manager(user):
        tm._require_role(user, "manager", "admin")      # keep the real role check
        return {"id": "10", "fields": {"Title": "Steve Park"}}, "Steve Park"

    async def require_teammate(employee_id, manager_name):
        if employee_id != "20":                         # only Omar is on Steve's team
            raise HTTPException(status_code=403, detail="That person is not on your team.")
        return TEAMMATE

    async def submit(request_type, form, employee, source, notify=True):
        seen["submitted"].append((request_type, form, employee, source, notify))
        return {"id": "501"}

    async def approve(request_type, item_id, approver_id="admin", channel="admin_dashboard"):
        seen["approved"].append((request_type, item_id, approver_id, channel))
        return seen["approve_result"]

    async def recover(item_id):
        seen["recovered"].append(item_id)
        return seen["recover_result"]

    async def not_paused():
        return None

    monkeypatch.setattr(tm, "_signed_in_manager", signed_in_manager)
    monkeypatch.setattr(tm, "_require_teammate", require_teammate)
    monkeypatch.setattr(tm, "submit_request", submit)
    monkeypatch.setattr(tm, "approve_new_request", approve)
    monkeypatch.setattr(tm, "_refuse_if_paused", not_paused)
    monkeypatch.setattr(tm, "RECOVER_HANDLERS", {"leave": recover, "overtime": recover, "carryover-payout": recover})
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    return seen


def _sick_day(**form) -> tm.TeamNewRequestBody:
    """A one-day sick request for Omar, with any form fields overridden."""
    base = {"leave_type": "Sick or Personal Day", "start_date": "2026-10-27", "end_date": "2026-10-27"}
    return tm.TeamNewRequestBody(request_type="leave", employee_id="20", form={**base, **form})


def test_a_teammates_request_is_made_and_approved_as_the_manager(stubbed):
    result = run(tm.team_new_request(STEVE, _sick_day(notes="called in at 7")))
    assert result == {"status": "approved", "request_type": "leave", "item_id": "501"}
    request_type, form, employee, source, notify = stubbed["submitted"][0]
    assert employee is TEAMMATE                        # in the teammate's name
    assert source == "Manager dashboard"
    assert notify is False                             # nobody is asked to approve it
    assert form.notes == "Made by Steve Park: called in at 7"   # the origin shows on the request
    # Approved under the manager's own id and channel, not the admin's.
    assert stubbed["approved"] == [("leave", "501", "10", "manager_dashboard")]


def test_a_request_with_no_note_still_says_who_made_it(stubbed):
    run(tm.team_new_request(STEVE, _sick_day()))
    assert stubbed["submitted"][0][1].notes == "Made by Steve Park"


def test_overtime_details_say_who_made_it(stubbed):
    body = tm.TeamNewRequestBody(
        request_type="overtime", employee_id="20",
        form={"description": "inventory count", "date": "2026-10-24", "hours": 3},
    )
    run(tm.team_new_request(STEVE, body))
    assert stubbed["submitted"][0][1].description == "Made by Steve Park: inventory count"


def test_a_non_teammate_is_refused_and_nothing_is_made(stubbed):
    body = tm.TeamNewRequestBody(request_type="leave", employee_id="99", form=_sick_day().form)
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(STEVE, body))
    assert err.value.status_code == 403
    assert stubbed["submitted"] == []


def test_a_manager_cannot_make_their_own_request_here(stubbed):
    body = tm.TeamNewRequestBody(request_type="leave", employee_id="10", form=_sick_day().form)
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(STEVE, body))
    assert err.value.status_code == 403
    assert stubbed["submitted"] == []


def test_an_employee_token_is_refused(stubbed):
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(DashboardUser(role="employee", user_id="20"), _sick_day()))
    assert err.value.status_code == 403


def test_carry_over_outside_hr_dates_is_refused(stubbed, monkeypatch):
    async def closed(request_type):
        raise RequestTypeClosed("Carry-over and payout requests closed on April 30, 2026.")

    monkeypatch.setattr(tm, "ensure_request_type_open", closed)
    body = tm.TeamNewRequestBody(
        request_type="carryover-payout", employee_id="20",
        form={"type_of_request": "Carry Over", "days": 2},
    )
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(STEVE, body))
    assert err.value.status_code == 409
    assert stubbed["submitted"] == []


def test_a_bad_form_is_refused_before_anything_is_saved(stubbed):
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(STEVE, _sick_day(end_date="2026-10-20")))   # ends before it starts
    assert err.value.status_code == 400
    assert stubbed["submitted"] == []


def test_processing_off_refuses(stubbed, monkeypatch):
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    with pytest.raises(HTTPException) as err:
        run(tm.team_new_request(STEVE, _sick_day()))
    assert err.value.status_code == 503


def test_a_failed_approval_falls_back_to_pending(stubbed):
    stubbed["approve_result"] = {"error": "It overlaps another request."}
    result = run(tm.team_new_request(STEVE, _sick_day()))
    assert result["status"] == "pending"
    assert stubbed["recovered"] == ["501"]             # handed to its manager the normal way
    assert "It overlaps another request." in result["message"]
    assert "waits for approval" in result["message"]


def test_new_request_options_report_the_carry_over_dates(stubbed, monkeypatch):
    async def window():
        return (date(2026, 11, 1), date(2026, 11, 30))

    monkeypatch.setattr(tm, "carryover_payout_window", window)
    monkeypatch.setattr(tm, "toronto_today", lambda: date(2026, 10, 27))
    result = run(tm.team_new_request_options(STEVE))
    assert "Sick or Personal Day" in result["leave_types"]
    assert result["carryover_payout_open"] is False
    assert result["carryover_closed_message"] == "Carry-over and payout requests open on November 1, 2026, until November 30, 2026."


def test_the_bc_only_sick_day_route_and_flag_are_gone():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/team/sick-days" not in paths and "/api/team/sick-days" not in paths
    assert not hasattr(settings, "SICK_DAY_ON_BEHALF_PROVINCES")
