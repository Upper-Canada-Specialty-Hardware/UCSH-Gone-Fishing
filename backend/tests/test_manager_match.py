"""Who manages whom, matched by id or by name, and the sign-in role refresh (#376).

A manager set up outside the app showed on the Manager assignments screen but
got no My team: every check compared names letter for letter, and the role was
fixed at sign-in for 30 days. These cover the shared matching rule and the
POST /api/intake/session/refresh route that re-checks the role on each visit.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import intake
from app.services.dashboard_tokens import generate_dashboard_token, validate_dashboard_token
from app.services.manager_match import (
    ManagerName,
    entry_keys,
    link_ids_for,
    lists_manager,
    normalise_name,
    team_names,
)


def _person(*managers: tuple[int, str]) -> dict:
    """Staff fields listing the given ``(LookupId, LookupValue)`` managers."""
    return {"AllManagers": [{"LookupId": i, "LookupValue": n} for i, n in managers]}


# ----- the matching rule -----


def test_names_match_ignoring_case_and_spacing():
    fields = _person((12, "Glenn  Clarke "))                    # SharePoint's spacing
    assert lists_manager(fields, "glenn clarke")
    assert normalise_name("  Glenn   CLARKE ") == "glenn clarke"


def test_id_matches_when_the_names_differ():
    fields = _person((12, "G. Clarke (IT)"))                    # Microsoft 365 name differs from staff name
    assert not lists_manager(fields, "Glenn Clarke")            # name alone misses
    assert lists_manager(fields, ManagerName("Glenn Clarke", frozenset({12})))   # the id finds him


def test_a_blank_manager_matches_nobody():
    assert not lists_manager(_person((0, "")), "")
    assert not lists_manager({}, ManagerName("Glenn Clarke", frozenset({12})))   # no managers at all


def test_team_map_is_found_by_name_or_id():
    team: dict = {}
    for report, entry in (("Ana", {"LookupId": 12, "LookupValue": "G. Clarke"}),
                          ("Lee", {"LookupId": 99, "LookupValue": "Glenn Clarke"})):
        for key in entry_keys(entry):
            team.setdefault(key, set()).add(report)
    assert team_names(team, ManagerName("Glenn Clarke", frozenset({12}))) == {"Ana", "Lee"}
    assert team_names(team, "glenn clarke") == {"Lee"}           # plain name: name matches only


def test_link_ids_before_and_after_the_switch():
    item = {"id": "596", "fields": {"Title": "Glenn Clarke", "TitleLinkLookupId": 12}}
    assert link_ids_for(item, postgres=False) == frozenset({12})         # Microsoft 365 id only
    assert link_ids_for(item, postgres=True) == frozenset({12, 596})     # plus the staff id
    assert link_ids_for({"id": "x", "fields": {}}, postgres=True) == frozenset()


# ----- POST /session/refresh -----


@pytest.fixture
def client(monkeypatch):
    """The intake router with the directory and the manager check stubbed.

    ``.records`` maps a staff id to its record; ``.managers`` holds the names
    someone lists as a manager; ``.fail`` makes the manager check raise.
    """
    app = FastAPI()
    app.include_router(intake.router, prefix="/api/intake")
    test_client = TestClient(app)
    test_client.records = {}
    test_client.managers = set()
    test_client.fail = False

    async def fake_by_id(uid):
        return test_client.records.get(str(uid))

    async def fake_is_manager(name):
        if test_client.fail:
            raise RuntimeError("SharePoint throttled")
        return normalise_name(name) in test_client.managers

    monkeypatch.setattr(intake, "get_employee_by_id", fake_by_id)
    monkeypatch.setattr(intake, "is_manager", fake_is_manager)
    return test_client


def _refresh(client, role: str, uid: str = "596"):
    """POST /session/refresh with a freshly signed token for role/uid."""
    return client.post("/api/intake/session/refresh", json=generate_dashboard_token(role, uid))


def test_an_employee_token_becomes_manager_once_someone_lists_them(client):
    client.records["596"] = {"id": "596", "fields": {"Title": "Glenn Clarke"}}
    client.managers.add("glenn clarke")
    body = _refresh(client, "employee").json()
    assert body["changed"] is True and body["role"] == "manager"
    valid, _ = validate_dashboard_token(body["role"], body["uid"], body["token"], body["exp"])
    assert valid                                                # a real signed token


def test_an_unchanged_role_keeps_the_token(client):
    client.records["596"] = {"id": "596", "fields": {"Title": "Glenn Clarke"}}
    client.managers.add("glenn clarke")
    assert _refresh(client, "manager").json() == {"changed": False}


def test_a_manager_with_nobody_under_them_is_never_demoted(client):
    # #395: the refresh only ever promotes. A supervisor opening the add-new-hire
    # link before anyone is under them, or an admin's open-as-manager link, keeps
    # the manager role (and Add Employee) even though nobody lists them yet.
    client.records["596"] = {"id": "596", "fields": {"Title": "Glenn Clarke"}}
    # client.managers stays empty: nobody lists Glenn as their manager.
    assert _refresh(client, "manager").json() == {"changed": False}


def test_a_failed_check_never_takes_the_role_away(client):
    client.records["596"] = {"id": "596", "fields": {"Title": "Glenn Clarke"}}
    client.fail = True
    assert _refresh(client, "manager").json() == {"changed": False}


def test_a_bad_token_is_refused(client):
    token = generate_dashboard_token("employee", "596")
    token["token"] = "forged"
    assert client.post("/api/intake/session/refresh", json=token).status_code == 401


def test_an_archived_person_gets_no_new_token(client):
    client.records["596"] = {"id": "596", "fields": {"Title": "Glenn Clarke", "ArchivedAt": "2026-10-01T00:00:00Z"}}
    client.managers.add("glenn clarke")
    assert _refresh(client, "employee").status_code == 403
