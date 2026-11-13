"""The automatic year-end carry-over, payout spend order and the carry-over refusal (#549).

Staff reads and writes, the lock and email are stubbed; the once-per-year log runs
on the SQLite test database.
"""
import asyncio

import pytest
from sqlalchemy import delete

from app.database import async_session
from app.models.year_end_carry_log import YearEndCarryLog
from app.services.balance import simulate_carryover_payout_impact
from app.services.request_intake import (
    CARRY_OVER_AUTOMATIC_MESSAGE,
    CarryoverPayoutForm,
    RequestFormError,
    submit_request,
)
from app.tasks import year_end_carry as yec


def run(coro):
    """Run one coroutine to completion in its own event loop."""
    return asyncio.run(coro)


# ----- the pure carry -----

def test_a_positive_leftover_moves_to_next_year_and_the_entitlement_refills():
    assert yec.carry_pure(4, 0, 15) == {"CurrentVacationBalance": 15, "CarryOver": 4, "moved": 4}


def test_days_already_set_aside_for_next_year_are_kept():
    # Carry-over approved for 2027 before the change stays and the leftover joins it.
    assert yec.carry_pure(3, 4, 15) == {"CurrentVacationBalance": 15, "CarryOver": 7, "moved": 3}


def test_a_negative_leftover_is_owed_from_the_new_entitlement():
    assert yec.carry_pure(-2, 0, 15) == {"CurrentVacationBalance": 13, "CarryOver": 0, "moved": 0}


def test_nothing_left_moves_nothing():
    assert yec.carry_pure(0, 0, 15) == {"CurrentVacationBalance": 15, "CarryOver": 0, "moved": 0}


# ----- the run -----

class FakeStaff:
    """A stand-in staff store holding a few people in memory."""

    def __init__(self, people: dict[str, dict]):
        self.people = people
        self.writes: list[tuple[str, dict, str]] = []

    async def get_list_items(self, _list_id):
        return [{"id": pid, "fields": dict(f)} for pid, f in self.people.items()]

    async def get_list_item(self, _list_id, pid):
        return {"id": pid, "fields": dict(self.people[pid])}

    async def update_list_item_fields(self, _list_id, pid, values, **history):
        self.people[pid].update(values)
        self.writes.append((pid, values, history.get("history_actor")))


@pytest.fixture
def wired(monkeypatch):
    """Stub the staff store, lock and email; clear the log."""
    async def clear():
        async with async_session() as session:
            await session.execute(delete(YearEndCarryLog))
            await session.commit()

    run(clear())
    staff = FakeStaff({
        "1": {"Title": "Avery", "EmailAddress": "a@x", "SalaryHourly": "Salary",
              "CurrentVacationBalance": 3, "CarryOver": 4, "DefaultYearlyVacationDays": 15,
              "CurrentSickDayBalance": 2, "SickDayEntitlement": 6, "Payout": 3},
        "2": {"Title": "Jamie", "EmailAddress": "j@x", "SalaryHourly": "Salary",
              "CurrentVacationBalance": -2, "CarryOver": 0, "DefaultYearlyVacationDays": 15,
              "CurrentSickDayBalance": -1, "SickDayEntitlement": 6, "Payout": 0},
        "3": {"Title": "Pat", "EmailAddress": "p@x", "SalaryHourly": "Hourly",
              "CurrentVacationBalance": 0, "CarryOver": 0, "DefaultYearlyVacationDays": 0,
              "CurrentSickDayBalance": 1, "SickDayEntitlement": 5, "Payout": 0},
    })
    sent: list[tuple[list, str]] = []

    async def fake_send(to, subject, html_body, **_kw):
        sent.append((to, subject))

    class AsyncNull:
        """An async context manager that does nothing, standing in for the lock."""

        async def __aenter__(self):
            return None

        async def __aexit__(self, *_a):
            return False

    class FakeLockManager:
        def lock(self, _key):
            return AsyncNull()

    async def no_admin(_name):
        return None

    monkeypatch.setattr(yec, "staff_store", staff)
    monkeypatch.setattr(yec, "send_email", fake_send)
    monkeypatch.setattr(yec, "lock_manager", FakeLockManager())
    monkeypatch.setattr(yec, "get_employee_by_name", no_admin)
    return staff, sent


def test_the_run_refreshes_everyone_once(wired):
    staff, sent = wired
    result = run(yec.run_year_end_carry(2026))
    avery, jamie, pat = staff.people["1"], staff.people["2"], staff.people["3"]
    assert (avery["CurrentVacationBalance"], avery["CarryOver"], avery["CurrentSickDayBalance"]) == (15, 7, 6)
    assert avery["Payout"] == 3                                       # payout is never reset; HR clears it by hand
    assert (jamie["CurrentVacationBalance"], jamie["CarryOver"], jamie["CurrentSickDayBalance"]) == (13, 0, 5)   # owed days come off
    assert pat["CurrentSickDayBalance"] == 5 and pat["CurrentVacationBalance"] == 0   # hourly: sick refill only
    assert "CurrentVacationBalance" not in dict((p, v) for p, v, _a in staff.writes)["3"]
    assert all(actor == "system" for _p, _v, actor in staff.writes)
    assert [to for to, _s in sent] == [["a@x"]]                       # only someone whose days moved
    assert sent[0][1] == "Your 2026 vacation days moved to 2027"
    assert len(result["carried"]) == 3

    run(yec.run_year_end_carry(2026))                                 # a second run changes nothing
    assert avery["CarryOver"] == 7 and len(staff.writes) == 3


def test_sick_refill_takes_off_days_owed_and_never_carries():
    assert yec.sick_refill_pure(-2, 6) == 4
    assert yec.sick_refill_pure(3, 6) == 6


def test_the_moved_email_names_the_deadline():
    body = yec.render_moved_email("Avery", 3, 2026)
    assert "3 days of your 2026 vacation moved to 2027" in body and "March 31, 2027" in body


# ----- payout takes the next-year line first -----

def test_a_payout_takes_the_next_year_line_first():
    after = simulate_carryover_payout_impact({"CurrentVacationBalance": 5, "CarryOver": 2, "Payout": 0}, 3, "Payout")
    assert after["CarryOver"] == 0 and after["CurrentVacationBalance"] == 4 and after["Payout"] == 3


def test_a_payout_is_refused_when_both_lines_fall_short():
    assert simulate_carryover_payout_impact({"CurrentVacationBalance": 1, "CarryOver": 1, "Payout": 0}, 3, "Payout") is None


# ----- a new carry-over is refused -----

def test_a_new_carry_over_is_refused_before_anything_is_written():
    form = CarryoverPayoutForm(type_of_request="Carry Over", days=1)
    with pytest.raises(RequestFormError) as err:
        run(submit_request("carryover-payout", form, {"fields": {"EmailAddress": "a@x"}}, "request_page"))
    assert str(err.value) == CARRY_OVER_AUTOMATIC_MESSAGE
