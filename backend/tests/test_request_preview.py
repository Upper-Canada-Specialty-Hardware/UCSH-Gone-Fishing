"""The request page's balance preview (#171).

The pure part is checked against the same rules approval uses; the route is
checked for auth, form errors, and that it writes nothing.
"""

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes import self_service
from app.services import request_preview
from app.services.dashboard_tokens import generate_dashboard_token
from app.services.request_intake import parse_request_form
from app.services.request_preview import build_preview

# Avery: some of every pot, salaried, in Ottawa (Ontario holidays).
AVERY = {
    "Title": "Avery Example", "Location": "Ottawa", "SalaryHourly": "Salary",
    "CurrentVacationBalance": 10, "CurrentSickDayBalance": 3, "CurrentOvertimeBalance": 1,
    "CarryOver": 0, "Payout": 0, "DefaultYearlyVacationDays": 15, "SickDayEntitlement": 6,
}
# A holiday on a Monday (2030-11-11) and a half-Friday season around it.
HOLIDAYS = [
    {"Title": "Remembrance Day", "Date": "2030-11-11"},
    {"Title": "Half Fridays START", "Date": "2030-06-01"},
    {"Title": "Half Fridays END", "Date": "2030-08-31"},
]


def _form(kind, **body):
    """A checked form, as the route would have it."""
    return parse_request_form(kind, body)


def test_vacation_skips_the_holiday_and_spends_make_up_first():
    form = _form("leave", leave_type="Vacation", start_date="2030-11-11", end_date="2030-11-13")
    out = build_preview("leave", form, AVERY, HOLIDAYS)
    assert out["days"] == 2                                     # Mon is a holiday: Tue and Wed
    assert "Remembrance Day is a company holiday" in out["notes"][0]
    assert out["projected"]["CurrentOvertimeBalance"] == 0      # Make-Up used first
    assert out["projected"]["CurrentVacationBalance"] == 9      # then 1 day of Vacation
    assert out["warning"] == "" and out["unchanged"] == ""


def test_sick_spends_sick():
    form = _form("leave", leave_type="Sick or Personal Day", start_date="2030-11-12", end_date="2030-11-12")
    out = build_preview("leave", form, AVERY, HOLIDAYS)
    assert out["projected"]["CurrentSickDayBalance"] == 2


def test_bereavement_changes_nothing():
    form = _form("leave", leave_type="Bereavement", start_date="2030-11-12", end_date="2030-11-14")
    out = build_preview("leave", form, AVERY, HOLIDAYS)
    assert out["projected"] is None
    assert out["unchanged"] == "Bereavement and jury duty do not use any balance."


def test_part_day_on_a_holiday_warns():
    form = _form("leave", leave_type="Half Day or Partial Day Off", start_date="2030-11-11", partial_hours=4)
    out = build_preview("leave", form, AVERY, HOLIDAYS)
    assert out["days"] == 0.5
    assert "rejected automatically" in out["warning"]


def test_part_day_over_four_hours_on_a_half_friday_warns():
    form = _form("leave", leave_type="Half Day or Partial Day Off", start_date="2030-07-05", partial_hours=6)
    out = build_preview("leave", form, AVERY, HOLIDAYS)
    assert "half-day Friday" in out["warning"]


def test_overtime_adds_hours_as_days_and_warns_on_a_holiday():
    form = _form("overtime", description="Inventory", date="2030-11-11", hours=4)
    out = build_preview("overtime", form, AVERY, HOLIDAYS)
    assert out["days"] == 0.5
    assert out["projected"]["CurrentOvertimeBalance"] == 1.5
    assert "Overtime on a holiday" in out["warning"]


def test_payout_beyond_vacation_warns():
    form = _form("carryover-payout", type_of_request="Payout", days=12)
    out = build_preview("carryover-payout", form, AVERY, HOLIDAYS)
    assert out["projected"] is None
    assert "fewer than 12" in out["warning"]


def test_carry_over_moves_vacation():
    form = _form("carryover-payout", type_of_request="Carry Over", days=2)
    out = build_preview("carryover-payout", form, AVERY, HOLIDAYS)
    assert out["projected"]["CarryOver"] == 2
    assert out["projected"]["CurrentVacationBalance"] == 8


def test_hourly_staff_keep_their_balances_except_sick():
    hourly = {**AVERY, "SalaryHourly": "Hourly"}
    vac = build_preview("leave", _form("leave", leave_type="Vacation", start_date="2030-11-12", end_date="2030-11-12"), hourly, [])
    assert vac["projected"] is None and "hourly" in vac["unchanged"]
    sick = build_preview("leave", _form("leave", leave_type="Sick or Personal Day", start_date="2030-11-12", end_date="2030-11-12"), hourly, [])
    assert sick["projected"]["CurrentSickDayBalance"] == 2


def test_next_year_leaves_vacation_alone():
    year = date.today().year + 1
    form = _form("leave", leave_type="Vacation", start_date=f"{year}-02-02", end_date=f"{year}-02-02")
    out = build_preview("leave", form, AVERY, [])
    assert out["next_year"] is True
    assert out["projected"]["CurrentVacationBalance"] == 10     # untouched
    assert out["projected"]["CurrentOvertimeBalance"] == 0


# ----- the route -----

@pytest.fixture
def client(monkeypatch):
    """The self-service router with the staff record and holidays stubbed; any write fails the test."""
    app = FastAPI()
    app.include_router(self_service.router, prefix="/api/dashboard")
    monkeypatch.setattr(settings, "PROCESSING_ENABLED", False)  # the preview works while processing is off

    async def by_id(item_id):
        return {"id": "7", "fields": AVERY} if str(item_id) == "7" else None

    async def holidays(province):
        return HOLIDAYS

    async def no_writes(*args, **kwargs):
        raise AssertionError("the preview must not create anything")

    monkeypatch.setattr(self_service, "get_employee_by_id", by_id)
    monkeypatch.setattr(request_preview, "get_holidays_for_province", holidays)
    monkeypatch.setattr(self_service, "submit_request", no_writes)
    return TestClient(app)


def _preview(client, kind, body, uid="7"):
    """POST a form to the preview with a valid employee token for ``uid``."""
    return client.post(f"/api/dashboard/me/requests/{kind}/preview", params=generate_dashboard_token("employee", uid), json=body)


def test_route_returns_dashboard_shaped_balances(client):
    res = _preview(client, "leave", {"leave_type": "Vacation", "start_date": "2030-11-12", "end_date": "2030-11-12"})
    assert res.status_code == 200
    data = res.json()
    assert data["current"]["vacation_balance"] == 10
    assert data["projected"]["overtime"] == 0                  # one day of Make-Up used
    assert data["projected"]["vacation_entitlement"] == 15     # untouched fields carried over


def test_route_rejects_a_bad_form(client):
    res = _preview(client, "overtime", {"description": "", "date": "2030-11-12", "hours": 2})
    assert res.status_code == 400


def test_route_needs_a_token(client):
    res = client.post("/api/dashboard/me/requests/leave/preview", json={})
    assert res.status_code in (401, 422)


def test_route_unknown_location_still_previews(client, monkeypatch):
    async def by_id(item_id):
        return {"id": "7", "fields": {**AVERY, "Location": "Nowhere"}}
    monkeypatch.setattr(self_service, "get_employee_by_id", by_id)
    monkeypatch.setattr(request_preview, "get_holidays_for_province", None)   # must not be reached usefully
    res = _preview(client, "leave", {"leave_type": "Vacation", "start_date": "2030-11-11", "end_date": "2030-11-11"})
    assert res.status_code == 200
    assert res.json()["days"] == 1                              # the holiday is not known, so it counts
    assert any("could not be checked" in n for n in res.json()["notes"])
