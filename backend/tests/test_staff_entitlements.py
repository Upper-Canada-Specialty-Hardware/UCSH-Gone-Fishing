"""The admin person panel's entitlement/balance editing (#272).

Two layers are covered. The pure ``plan_changes`` validation is tested on its
own with plain dicts (no I/O). The async service and the two routes are tested
with the SharePoint client, the employee read, the request-allow recalculation
and the history functions monkeypatched, so the real service logic runs while
nothing touches Graph or the database.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes.dashboard import router as dashboard_router
from app.services import staff_entitlements as se


def run(coro):
    """Run one coroutine to completion."""
    return asyncio.run(coro)


def _record() -> dict:
    """A Staff Directory record standing in for one person.

    Returns:
        The SharePoint response shape ``{"id", "fields"}`` with the editable
        figures plus one non-panel column (AllManagers) to prove filtering.
    """
    return {
        "id": "480",
        "fields": {
            "Title": "Dana Lee",
            "EmailAddress": "dana@ucsh.com",
            "CurrentVacationBalance": 10,            # vacation pot
            "CurrentSickDayBalance": 5,              # sick pot
            "CurrentOvertimeBalance": 2.125,         # make-up pot, fractional
            "CarryOver": 0,                          # carried-over vacation
            "Payout": 0,                             # banked for payout
            "DefaultYearlyVacationDays": 15,         # vacation entitlement
            "SickDayEntitlement": 5,                 # sick entitlement
            "AllManagers": [{"LookupValue": "Boss One"}],  # NOT a panel column
        },
    }


# The current field values used by the pure planner tests.
CURRENT = _record()["fields"]


# ---------------------------------------------------------------------------
# plan_changes: pure validation and diffing, no I/O
# ---------------------------------------------------------------------------

def test_plan_rejects_an_unknown_key():
    """A key outside EDITABLE is refused outright."""
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"bonus_days": 5})


def test_plan_rejects_a_non_number():
    """A value that is not a number is refused."""
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"vacation": "lots"})


def test_plan_rejects_nan_and_inf():
    """NaN and infinity are not real figures and are refused."""
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"vacation": float("nan")})
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"vacation": float("inf")})


def test_plan_rejects_a_bool():
    """A bool (an int subclass) is not a valid figure and is refused."""
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"vacation": True})


def test_plan_rejects_entitlement_at_zero_or_below():
    """Yearly grants must stay above zero."""
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"vacation_entitlement": 0})
    with pytest.raises(ValueError):
        se.plan_changes(CURRENT, {"sick_entitlement": -1})


def test_plan_allows_negative_and_fractional_balances():
    """Balances (unlike grants) may be negative or fractional."""
    changes = se.plan_changes(CURRENT, {"vacation": -3, "make_up": 2.5})
    by_field = {c["field"]: c for c in changes}      # index by wire key
    assert by_field["vacation"]["after"] == -3       # negative balance allowed
    assert by_field["make_up"]["after"] == 2.5       # fractional balance allowed
    assert by_field["make_up"]["column"] == "CurrentOvertimeBalance"  # mapped column


def test_plan_drops_unchanged_values():
    """Values equal to the current record are not part of the diff."""
    # vacation already 10 and sick already 5, so only payout moves.
    changes = se.plan_changes(CURRENT, {"vacation": 10, "sick": 5, "payout": 4})
    assert [c["field"] for c in changes] == ["payout"]


def test_plan_treats_a_blank_current_value_as_zero():
    """A missing or blank current column counts as zero when diffing."""
    current = {"CarryOver": ""}                       # blank stored value
    assert se.plan_changes(current, {"carry_over": 0}) == []  # 0 == blank -> no-op
    changes = se.plan_changes(current, {"carry_over": 2})
    assert changes[0]["before"] == 0.0 and changes[0]["after"] == 2.0


# ---------------------------------------------------------------------------
# apply_entitlement_changes: the async write path, primitives monkeypatched
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(monkeypatch):
    """Replace the service's primitives with spies and capture every write.

    Returns:
        A dict with ``updates``, ``recalc`` and ``history`` lists recording the
        calls the service made, so a test can assert exactly what happened.
    """
    calls = {"updates": [], "recalc": [], "history": []}

    async def fake_get(emp_id):
        # Only id 480 exists; anything else is unknown.
        return _record() if str(emp_id) == "480" else None

    async def fake_update(list_id, item_id, fields):
        calls["updates"].append((list_id, str(item_id), fields))

    async def fake_recalc(emp_id, vacation, carryover):
        calls["recalc"].append((str(emp_id), vacation, carryover))

    async def fake_record(emp_id, name, changes, reason, source="admin_dashboard"):
        calls["history"].append(
            {"emp": str(emp_id), "name": name, "changes": changes,
             "reason": reason, "source": source}
        )
        return len(changes)

    monkeypatch.setattr(se, "get_employee_by_id", fake_get)
    monkeypatch.setattr(se, "recalculate_request_allow_date", fake_recalc)
    monkeypatch.setattr(se.sp_client, "update_list_item_fields", fake_update)
    monkeypatch.setattr(se.staff_changes, "record_changes", fake_record)
    return calls


def test_only_differing_columns_are_written_in_one_call(wired):
    """A single update carries exactly the columns that moved."""
    result = run(se.apply_entitlement_changes(
        "480", {"vacation": 12, "sick": 5, "payout": 3}, "  correction  "
    ))
    assert len(wired["updates"]) == 1                # one patch, not one per field
    _, item_id, fields = wired["updates"][0]
    assert item_id == "480"
    assert fields == {"CurrentVacationBalance": 12.0, "Payout": 3.0}  # sick dropped
    assert result["history_recorded"] is True
    assert {c["field"] for c in result["changed"]} == {"vacation", "payout"}


def test_recalc_runs_only_when_vacation_or_carryover_moves(wired):
    """The request-allow date is recomputed only for vacation/carry-over moves."""
    run(se.apply_entitlement_changes("480", {"payout": 9}, "x"))   # payout only
    assert wired["recalc"] == []                     # no recalc for a payout change
    run(se.apply_entitlement_changes("480", {"carry_over": 4}, "x"))
    # vacation unchanged (10 from the record), carry-over new (4).
    assert wired["recalc"] == [("480", 10.0, 4.0)]


def test_recalc_uses_the_new_vacation_and_the_current_carryover(wired):
    """A vacation change passes the new vacation and the record's carry-over."""
    run(se.apply_entitlement_changes("480", {"vacation": 0}, "zeroed"))
    assert wired["recalc"] == [("480", 0.0, 0.0)]    # new vacation 0, carry-over 0


def test_history_gets_column_before_after_tuples_and_a_stripped_reason(wired):
    """History is recorded as (column, before, after) with the trimmed reason."""
    run(se.apply_entitlement_changes(
        "480", {"vacation": 12, "make_up": 3}, "   fix overtime   "
    ))
    assert len(wired["history"]) == 1
    entry = wired["history"][0]
    assert entry["reason"] == "fix overtime"         # surrounding whitespace gone
    assert entry["emp"] == "480" and entry["name"] == "Dana Lee"
    assert ("CurrentVacationBalance", 10.0, 12.0) in entry["changes"]
    assert ("CurrentOvertimeBalance", 2.125, 3.0) in entry["changes"]


def test_a_history_failure_keeps_the_write_and_flags_it(wired, monkeypatch):
    """When history recording fails, the SharePoint write is kept and reported."""
    async def boom(*a, **k):
        raise RuntimeError("history table down")

    monkeypatch.setattr(se.staff_changes, "record_changes", boom)
    result = run(se.apply_entitlement_changes("480", {"vacation": 1}, "x"))
    assert len(wired["updates"]) == 1                # the write still happened
    assert result["history_recorded"] is False
    assert result["notice"] == "The change was saved, but its history could not be recorded."


def test_a_noop_request_writes_nothing(wired):
    """Requesting the values already on record writes, recalcs and records nothing."""
    result = run(se.apply_entitlement_changes("480", {"vacation": 10, "sick": 5}, "same"))
    assert wired["updates"] == [] and wired["recalc"] == [] and wired["history"] == []
    assert result["changed"] == [] and result["history_recorded"] is True


def test_a_blank_reason_and_an_empty_request_are_refused(wired):
    """Both a whitespace-only reason and an empty change set raise ValueError."""
    with pytest.raises(ValueError):
        run(se.apply_entitlement_changes("480", {"vacation": 1}, "   "))
    with pytest.raises(ValueError):
        run(se.apply_entitlement_changes("480", {}, "a reason"))


def test_an_unknown_employee_raises_lookup_error(wired):
    """A missing record raises LookupError (the route turns it into a 404)."""
    with pytest.raises(LookupError):
        run(se.apply_entitlement_changes("999", {"vacation": 1}, "x"))


# ---------------------------------------------------------------------------
# The two routes: real service, primitives monkeypatched via `wired`
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """The dashboard router mounted bare, with processing enabled.

    Returns:
        A TestClient; server exceptions surface as 500s rather than re-raising.
    """
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", True)
    app = FastAPI()
    app.include_router(dashboard_router)
    return TestClient(app, raise_server_exceptions=False)


def test_patch_route_applies_a_change(client, wired):
    """A valid PATCH returns 200 and writes once."""
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"changes": {"vacation": 12}, "reason": "correction"},
    )
    assert resp.status_code == 200
    assert resp.json()["history_recorded"] is True
    assert len(wired["updates"]) == 1


def test_patch_route_is_503_when_processing_is_off(client, wired, monkeypatch):
    """With processing disabled the PATCH is refused before any write."""
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"changes": {"vacation": 12}, "reason": "x"},
    )
    assert resp.status_code == 503
    assert wired["updates"] == []


def test_patch_route_blank_reason_is_400(client, wired):
    """A whitespace-only reason surfaces as a readable 400."""
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"changes": {"vacation": 12}, "reason": "  "},
    )
    assert resp.status_code == 400


def test_patch_route_invalid_value_is_400(client, wired):
    """A zero entitlement is refused by the planner and becomes a 400."""
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"changes": {"vacation_entitlement": 0}, "reason": "x"},
    )
    assert resp.status_code == 400


def test_patch_route_unknown_employee_is_404(client, wired):
    """An unknown id becomes a 404."""
    resp = client.patch(
        "/admin/employees/999/entitlements",
        json={"changes": {"vacation": 12}, "reason": "x"},
    )
    assert resp.status_code == 404


def test_get_route_returns_panel_people_and_history(client, wired, monkeypatch):
    """GET returns only the panel columns, the resolved people, and history."""
    async def fake_list(emp_id, limit=100):
        return [{"field": "CurrentVacationBalance", "old_value": 10,
                 "new_value": 12, "reason": "x", "source": "admin_dashboard",
                 "changed_at": "2026-10-14T00:00:00"}]

    async def fake_name(value):
        return "Someone" if value else ""            # blank id -> empty name

    async def fake_managers(emp):
        return [{"fields": {"Title": "Boss One"}}, {"fields": {"Title": "Boss Two"}}]

    monkeypatch.setattr(se.staff_changes, "list_changes", fake_list)
    monkeypatch.setattr(se, "resolve_person_field_name", fake_name)
    monkeypatch.setattr(se, "get_all_managers_for_employee", fake_managers)

    resp = client.get("/admin/employees/480")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["fields"]) <= set(se.PANEL_COLUMNS)   # only panel columns
    assert "AllManagers" not in body["fields"]            # non-panel column dropped
    assert body["fields"]["Title"] == "Dana Lee"
    assert body["people"]["managers"] == ["Boss One", "Boss Two"]
    assert body["history"][0]["new_value"] == 12


def test_get_route_unknown_employee_is_404(client, wired):
    """GET on an unknown id is a 404."""
    assert client.get("/admin/employees/999").status_code == 404


def test_get_route_degrades_when_person_lookups_fail(client, wired, monkeypatch):
    """A failing person/history lookup leaves that part empty, not the whole read."""
    async def boom(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(se, "resolve_person_field_name", boom)
    monkeypatch.setattr(se, "get_all_managers_for_employee", boom)
    monkeypatch.setattr(se.staff_changes, "list_changes", boom)

    resp = client.get("/admin/employees/480")
    assert resp.status_code == 200                        # whole read still succeeds
    body = resp.json()
    assert body["people"] == {"title_link": "", "supervisor_link": "", "managers": []}
    assert body["history"] == []
    assert body["fields"]["Title"] == "Dana Lee"         # panel data still present


# ---------------------------------------------------------------------------
# plan_field_changes: pure Staff Directory field validation and diffing (#276)
# ---------------------------------------------------------------------------

def test_field_plan_diffs_text_and_drops_noops():
    """Only text fields that moved are returned; unchanged ones are dropped."""
    current = {"EmailAddress": "dana@ucsh.com", "Location": "Toronto Warden"}
    changes = se.plan_field_changes(
        current, {}, {"email": "dana@ucsh.com", "location": "Ottawa", "department": "Door"}
    )
    by_field = {c["field"]: c for c in changes}
    assert "email" not in by_field                        # unchanged email dropped
    assert by_field["location"]["column"] == "Location"   # moved location kept
    assert by_field["location"]["before"] == "Toronto Warden"
    assert by_field["location"]["after"] == "Ottawa"
    assert by_field["location"]["sp"] == {"Location": "Ottawa"}
    assert by_field["department"]["before"] == ""         # blank current -> empty string
    assert by_field["department"]["after"] == "Door"


def test_field_plan_blank_name_is_refused():
    """The display name is mandatory, so a whitespace-only name raises."""
    with pytest.raises(ValueError):
        se.plan_field_changes({"Title": "Dana Lee"}, {}, {"name": "   "})


def test_field_plan_unknown_key_is_refused():
    """A key that is neither a pot nor a known field raises."""
    with pytest.raises(ValueError):
        se.plan_field_changes({}, {}, {"nickname": "D"})


def test_field_plan_person_link_uses_lookup_id_and_name_history():
    """A person link writes its LookupId companion and records display names."""
    changes = se.plan_field_changes(
        {}, {"title_link": "Old Person"},
        {"title_link": {"sp_user_id": 42, "name": "New Person"}},
    )
    assert len(changes) == 1
    c = changes[0]
    assert c["column"] == "TitleLink"
    assert c["sp"] == {"TitleLinkLookupId": 42}           # written through the companion
    assert c["before"] == "Old Person" and c["after"] == "New Person"


def test_field_plan_person_link_clear_sets_none():
    """Clearing a person link sends None to the LookupId companion."""
    changes = se.plan_field_changes(
        {}, {"supervisor_link": "Someone"},
        {"supervisor_link": {"sp_user_id": None, "name": ""}},
    )
    assert changes[0]["sp"] == {"SupervisorLinkLookupId": None}
    assert changes[0]["after"] == ""


def test_field_plan_managers_compare_as_a_set():
    """Managers diff ignores order and writes the collection id list."""
    before = {"managers": ["Boss One"]}
    same = se.plan_field_changes({}, before, {"managers": {"ids": [7], "names": ["Boss One"]}})
    assert same == []                                     # same set -> no change
    changed = se.plan_field_changes(
        {}, before, {"managers": {"ids": [7, 8], "names": ["Boss One", "Boss Two"]}}
    )
    assert len(changed) == 1
    c = changed[0]
    assert c["column"] == "AllManagers"
    assert c["sp"]["AllManagersLookupId"] == [7, 8]
    assert c["sp"]["AllManagersLookupId@odata.type"] == "Collection(Edm.Int32)"
    assert c["before"] == "Boss One" and c["after"] == "Boss One, Boss Two"


# ---------------------------------------------------------------------------
# apply_entitlement_changes with directory fields (#276)
# ---------------------------------------------------------------------------

def test_pots_and_fields_save_in_one_call(wired):
    """A balance pot and a text field save together in a single update."""
    result = run(se.apply_entitlement_changes(
        "480", {"vacation": 12}, "name correction", {"email": "dana.lee@ucsh.com", "name": "Dana Li"}
    ))
    assert len(wired["updates"]) == 1                     # one patch for both
    _, _, fields = wired["updates"][0]
    assert fields == {
        "CurrentVacationBalance": 12.0,
        "EmailAddress": "dana.lee@ucsh.com",
        "Title": "Dana Li",
    }
    assert {c["field"] for c in result["changed"]} == {"vacation", "email", "name"}
    # History carries the column names, numbers for the pot, strings for the fields.
    recorded = {t[0]: (t[1], t[2]) for t in wired["history"][0]["changes"]}
    assert recorded["CurrentVacationBalance"] == (10.0, 12.0)
    assert recorded["EmailAddress"] == ("dana@ucsh.com", "dana.lee@ucsh.com")


def test_fields_only_save_needs_no_changes_key(wired):
    """A field-only save (no numeric pots) still writes and records history."""
    result = run(se.apply_entitlement_changes("480", {}, "moved office", {"location": "Ottawa"}))
    assert wired["updates"][0][2] == {"Location": "Ottawa"}
    assert [c["field"] for c in result["changed"]] == ["location"]
    assert ("Location", "", "Ottawa") in wired["history"][0]["changes"]


def test_a_person_link_save_resolves_the_before_name(wired, monkeypatch):
    """A person-link save resolves the current name for the history before value."""
    async def fake_name(value):
        return "Old Link"                                 # the resolver found the current link

    monkeypatch.setattr(se, "resolve_person_field_name", fake_name)
    run(se.apply_entitlement_changes(
        "480", {}, "fixed link", {"title_link": {"sp_user_id": 9, "name": "New Link"}}
    ))
    assert wired["updates"][0][2] == {"TitleLinkLookupId": 9}
    assert ("TitleLink", "Old Link", "New Link") in wired["history"][0]["changes"]


def test_route_accepts_a_fields_only_body(client, wired):
    """The PATCH route accepts a body with only fields and no changes key."""
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"fields": {"email": "new@ucsh.com"}, "reason": "update"},
    )
    assert resp.status_code == 200
    assert wired["updates"][0][2] == {"EmailAddress": "new@ucsh.com"}


def test_route_blank_name_field_is_400(client, wired):
    """A blank name in the fields body surfaces as a readable 400."""
    resp = client.patch(
        "/admin/employees/480/entitlements",
        json={"fields": {"name": "  "}, "reason": "x"},
    )
    assert resp.status_code == 400
