"""Add employee on a name already on staff edits that record (#444).

A held request's Add link opens the record that already has the person's name
as a profile edit. These tests pin who may see and save it (only the supervisor
holding the request, or an admin acting for them), what the save sends to the
staff service (the form's fields, the managers set by "replace" or
"alongside", a reason naming who and why), the notices (the replaced address,
every manager taken off), and when the held request is released (only once the
record carries the address the request came from; otherwise it stays held with
a note).

The staff directory, the staff service, email and the release are faked; held
rows are real rows in the test database.
"""
import asyncio
import time

import pytest

from app.database import async_session
from app.models import HeldRequest
from app.models.mixins import utcnow
from app.services import held_profile, held_requests


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


STEVE = {"id": "3", "fields": {"Title": "Steve Faith", "EmailAddress": "stevef@ucsh.com"}}
FAREEZ = {"id": "7", "fields": {"Title": "Fareez Vellani", "EmailAddress": "fareezv@ucsh.com"}}


def _anthony(email="anthony.old@ucsh.com"):
    """Anthony on staff, managed by Fareez (lookup id 7)."""
    return {"id": "412", "fields": {
        "Title": "Anthony Talic", "EmailAddress": email, "Department": "Production",
        "SalaryHourly": "Hourly", "Location": "Toronto Warden",
        "AllManagers": [{"LookupId": 7, "LookupValue": "Fareez Vellani"}],
        "DefaultYearlyVacationDays": 0, "SickDayEntitlement": 5,
        "CurrentVacationBalance": 0, "CurrentSickDayBalance": 2,
    }}


def _held(name="Anthony Talic", status="held", supervisor_id="3", earlier=()) -> int:
    """Store one held request from a fresh address and return its id.

    ``earlier`` lists supervisor ids it was sent to (and handed back by) first.
    """
    email = f"anthony{time.time_ns()}@ucsh.com"
    involved = [{"id": e, "name": "Earlier", "email": f"{e}@ucsh.com", "outcome": "not_on_team"} for e in earlier]

    async def add():
        async with async_session() as session:
            row = HeldRequest(
                email=email, name=name, department="Project Management",
                supervisor_id=supervisor_id, supervisor_name="Steve Faith", supervisor_email="stevef@ucsh.com",
                supervisors_involved=involved + [{"id": supervisor_id, "name": "Steve Faith",
                                                  "email": "stevef@ucsh.com", "outcome": None}],
                request_type="leave",
                form_data={"leave_type": "Vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"},
                status=status, created_at=utcnow(),
            )
            session.add(row)
            await session.commit()
            return row.id
    return run(add())


async def _row(held_id):
    async with async_session() as session:
        return await session.get(HeldRequest, held_id)


@pytest.fixture
def staff(monkeypatch):
    """The directory, the staff service, email and the release, faked.

    Yields:
        A dict: "records" (the directory), "saved" (staff service calls),
        "sent" (emails) and "released" (release calls).
    """
    world = {"records": [STEVE, FAREEZ, _anthony()], "saved": [], "sent": [], "released": []}

    class Store:
        async def get_list_items(self, list_id, **kwargs):
            return world["records"]

    def by(key, value):
        return next((r for r in world["records"] if (r["fields"].get(key) or "").lower() == (value or "").lower()), None)

    async def by_id(item_id):
        return next((r for r in world["records"] if r["id"] == str(item_id)), None)

    async def by_email(email):
        return by("EmailAddress", email)

    async def by_name(name):
        return by("Title", name)

    async def on_postgres():
        return True

    async def save(employee_id, changes, reason, fields, source, managers_from=None, actor=None):
        record = next(r for r in world["records"] if r["id"] == employee_id)
        if managers_from is not None:                          # worked out from the record as it is now
            fields = {**fields, "managers": managers_from(record["fields"])}
        world["saved"].append({"id": employee_id, "changes": changes, "reason": reason,
                               "fields": fields, "source": source, "actor": actor})
        if "email" in fields:                                   # the record now carries the new address
            next(r for r in world["records"] if r["id"] == employee_id)["fields"]["EmailAddress"] = fields["email"]
        return {"employee_id": employee_id, "name": "Anthony Talic", "changed": [{"field": "x"}],
                "history_recorded": True}

    async def send(to, subject, html_body, **kwargs):
        world["sent"].append({"to": to, "subject": subject, "html": html_body})

    async def release(email, only_id=None):
        world["released"].append((email, only_id))
        return [held_requests.ReleaseResult(only_id, "released", "3960")]

    monkeypatch.setattr(held_profile, "staff_store", Store())
    monkeypatch.setattr(held_profile, "get_employee_by_id", by_id)
    monkeypatch.setattr(held_profile, "get_employee_by_email", by_email)
    monkeypatch.setattr(held_profile, "get_employee_by_name", by_name)
    monkeypatch.setattr(held_profile, "staff_in_postgres", on_postgres)
    monkeypatch.setattr(held_profile, "apply_entitlement_changes", save)
    monkeypatch.setattr(held_profile, "send_email", send)
    monkeypatch.setattr(held_profile.held_requests, "release_held_requests", release)
    return world


def _update(held_id, supervisor_id="3", **overrides):
    """Save a profile edit with sensible defaults."""
    args = dict(actor_name="Steve Faith", supervisor_id=supervisor_id, changes={"vacation_entitlement": 10},
                fields={"department": "Project Management"}, manager_mode="replace",
                reason="moved to project management", source="manager_dashboard")
    args.update(overrides)
    return run(held_profile.update_existing_from_held(held_id, **args))


def test_the_holder_sees_the_record_with_that_name(staff):
    held_id = _held()
    found = run(held_profile.held_for_add(held_id, "3"))
    assert found["state"] == "open" and found["same_name_count"] == 1
    assert found["existing"]["employee_id"] == "412" and found["existing"]["managers"] == ["Fareez Vellani"]
    assert found["existing"]["sick"] == 2.0


def test_an_earlier_supervisor_learns_only_that_it_moved_on(staff):
    held_id = _held(earlier=("4",))                                # Eduardo handed it back, Steve holds it
    found = run(held_profile.held_for_add(held_id, "4"))
    assert found["state"] == "declined" and found["existing"] is None
    # Nothing about where it went or the person's details.
    assert found["supervisor_name"] == "" and found["email"] == "" and found["summary"] == ""


def test_a_manager_it_was_never_sent_to_reads_not_found(staff):
    assert run(held_profile.held_for_add(_held(), "99")) is None


def test_an_admin_acts_for_the_holder(staff):
    found = run(held_profile.held_for_add(_held(), None))
    assert found["state"] == "open" and found["existing"]["employee_id"] == "412"


def test_replace_saves_the_form_and_makes_the_supervisor_the_only_manager(staff):
    held_id = _held()
    email = run(_row(held_id)).email
    result = _update(held_id, fields={"email": email.upper(), "department": "Project Management"})

    [saved] = staff["saved"]
    assert saved["id"] == "412" and saved["source"] == "manager_dashboard"
    assert saved["fields"]["email"] == email                       # cleaned
    assert saved["fields"]["managers"] == {"ids": [3], "names": ["Steve Faith"]}
    assert saved["reason"] == "Changed by Steve Faith from Anthony Talic's held request: moved to project management"

    subjects = {m["subject"]: m["to"] for m in staff["sent"]}
    assert subjects["Your staff email was changed"] == ["anthony.old@ucsh.com"]   # the old address is told
    assert subjects["Anthony Talic moved off your team"] == ["fareezv@ucsh.com"]  # Fareez is told
    assert staff["released"] == [(email, held_id)] and result["held_status"] == "released"


def test_alongside_keeps_the_managers_on_record(staff):
    held_id = _held()
    # A manager added after the page opened is kept: the list is built from the record at save time.
    anthony = next(r for r in staff["records"] if r["id"] == "412")
    anthony["fields"]["AllManagers"].append({"LookupId": 9, "LookupValue": "Dana Quinn"})
    _update(held_id, manager_mode="alongside")
    [saved] = staff["saved"]
    assert saved["fields"]["managers"] == {"ids": [7, 9, 3], "names": ["Fareez Vellani", "Dana Quinn", "Steve Faith"]}
    assert not any("moved off" in m["subject"] for m in staff["sent"])           # nobody was taken off


def test_a_record_without_the_requests_address_stays_held_with_a_note(staff):
    held_id = _held()
    result = _update(held_id)                                      # email not changed
    assert staff["released"] == [] and result["held_status"] == "held"
    assert "its email is not" in result["note"]
    assert "its email is not" in run(_row(held_id)).last_error     # the admin list shows why
    assert not any(m["subject"] == "Your staff email was changed" for m in staff["sent"])


def test_only_the_holder_may_save(staff):
    held_id = _held()
    with pytest.raises(held_profile.HeldProfileError) as err:
        _update(held_id, supervisor_id="4")
    assert err.value.status_code == 409 and staff["saved"] == []


def test_the_supervisors_own_record_is_refused_even_for_an_admin(staff):
    held_id = _held(name="Steve Faith")                            # someone typed the supervisor's own name
    with pytest.raises(held_profile.HeldProfileError, match="own record"):
        _update(held_id, supervisor_id=None, actor_name="An admin")
    assert staff["saved"] == []


def test_a_closed_request_cannot_be_saved(staff):
    with pytest.raises(held_profile.HeldProfileError) as err:
        _update(_held(status="not_on_team"))
    assert err.value.status_code == 409


def test_two_records_with_the_name_are_refused(staff):
    staff["records"].append({"id": "500", "fields": {"Title": "anthony  talic", "EmailAddress": "a2@ucsh.com"}})
    held_id = _held()
    assert run(held_profile.held_for_add(held_id, "3"))["existing"] is None
    with pytest.raises(held_profile.HeldProfileError, match="Two people"):
        _update(held_id)
    assert staff["saved"] == []


@pytest.mark.parametrize("overrides, message", [
    ({"reason": "  "}, "reason is required"),
    ({"manager_mode": "swap"}, "replace"),
    ({"fields": {"name": "Someone Else"}}, "cannot be changed"),
    ({"changes": {"payout": 3}}, "cannot be changed"),
    ({"fields": {"email": "fareezv@ucsh.com"}}, "already on Fareez Vellani's record"),
])
def test_bad_edits_are_refused_before_anything_is_written(staff, overrides, message):
    with pytest.raises(held_profile.HeldProfileError, match=message):
        _update(_held(), **overrides)
    assert staff["saved"] == [] and staff["sent"] == []


def test_the_add_link_names_the_held_request():
    url = held_requests.add_employee_url("3", "Lee New", "lee@gmail.com", "Site Services", 42)
    assert "add_held=42" in url
