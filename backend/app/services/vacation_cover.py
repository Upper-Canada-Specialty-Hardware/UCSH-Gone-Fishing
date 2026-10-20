"""Route an approval past managers who are off, and list the ones nobody covers (#342).

The owner's ruling (2026-10-19): a request's managers are an ordered list (the
``AllManagers`` link order), and the approval email and text go to ONLY the
first manager on that list who is not off that day. Managers after the first
available one are not asked unless the earlier ones are off. A manager counts as
"off" when they have an APPROVED full-day leave request of any leave type
(vacation, sick, bereavement, and so on) whose dates cover the day in Toronto;
a part-day or half-day leave does not count, and a pending leave does not count.

This module is the one place that decides "off", so the submission path (#369),
the reminder re-decision and the admin cover list (#370) all agree. It reads the
Postgres request rows; new behaviour only needs to work on the Postgres path
(the pr-preview environment), per the round's contract.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.request_record import RequestRecord
from app.services.employment import PARTIAL_DAY

# The company calendar day: "off that day" is tested in Toronto, like every other
# window in the app (carry-over dates, the April-1 reset).
TORONTO = ZoneInfo("America/Toronto")

# The request kind -> the URL segment the dashboard approve/reject calls use. Only
# "carryover_payout" differs from its kind (the hyphen).
URL_SEGMENT = {"leave": "leave", "overtime": "overtime", "carryover_payout": "carryover-payout"}


def today_toronto() -> date:
    """Today's calendar date in Toronto, the day an approval is routed on.

    Returns:
        The Toronto date right now.
    """
    return datetime.now(TORONTO).date()


def _num(value) -> str:
    """A stored number as a short string: 3.0 reads "3", 2.5 reads "2.5".

    Args:
        value: A days/hours value, or None.

    Returns:
        The trimmed number, or "0" when blank.
    """
    try:
        return f"{float(value or 0):g}"                           # :g drops a trailing .0
    except (TypeError, ValueError):
        return "0"


def _fmt_date(day: date) -> str:
    """One date in the house format, "Nov 2, 2026".

    Args:
        day: The date to format.

    Returns:
        The formatted date.
    """
    return f"{day.strftime('%b')} {day.day}, {day.year}"          # %b is "Nov"; day drops the zero pad


def _return_date(intervals: list[tuple[date, date | None]], on_date: date) -> date | None:
    """The day a person is back, when an approved full-day leave covers ``on_date``.

    Pure: it only inspects the leave intervals handed to it, so the same rule
    serves one manager (the submission path) and every manager at once (the admin
    list). When several leaves cover the day, the latest end wins, so a run of
    back-to-back leaves reports the real return day.

    Args:
        intervals: ``(start, end)`` pairs of the person's approved full-day
            leaves; ``end`` may be None for a one-day leave.
        on_date: The day being tested.

    Returns:
        The day after the covering leave ends (when they are back), or None when
        no approved full-day leave covers ``on_date`` (they are not off).
    """
    back: date | None = None
    for start, end in intervals:
        end = end or start                                        # a one-day leave carries no end
        if start <= on_date <= end:                               # the leave covers the tested day
            day_after = end + timedelta(days=1)                   # they are back the day after it ends
            if back is None or day_after > back:                  # the latest-ending covering leave wins
                back = day_after
    return back


async def _manager_leaves(session, manager_staff_id) -> list[tuple[date, date | None]]:
    """The approved full-day leave intervals a manager has, as (start, end) pairs.

    A manager is matched to their own staff row by id (``manager_staff_id``),
    which is the id the ``AllManagers`` name already resolved to upstream, so this
    reuses the name match the rest of the code uses. Part-day leave is excluded:
    it does not make the manager unavailable.

    Args:
        session: An open session.
        manager_staff_id: The manager's Staff Directory id.

    Returns:
        The intervals; empty when the manager has no approved full-day leave.
    """
    rows = (await session.execute(
        select(RequestRecord.start_date, RequestRecord.end_date).where(
            RequestRecord.kind == "leave",
            RequestRecord.status == "approved",                   # pending leave does not count
            RequestRecord.employee_id == str(manager_staff_id),   # the manager, as the person on leave
            RequestRecord.leave_type != PARTIAL_DAY,              # a half/part day does not count
        )
    )).all()
    return [(start, end) for start, end in rows if start is not None]


async def off_until(session, manager_staff_id, on_date: date) -> date | None:
    """When a manager returns if they are off on ``on_date``, else None.

    Args:
        session: An open session.
        manager_staff_id: The manager's Staff Directory id.
        on_date: The day being tested (Toronto).

    Returns:
        The return date when the manager is off, or None when they are available.
    """
    return _return_date(await _manager_leaves(session, manager_staff_id), on_date)


async def choose_available_manager(session, managers: list[dict], on_date: date) -> tuple[dict | None, list[dict]]:
    """Pick the first manager not off on ``on_date``; report the off ones skipped.

    Walks the ordered manager list and returns the first manager who is not off.
    The managers before them (who are off, so the ask moves past them) are
    reported so the caller can record who was skipped and why. When every manager
    is off, no one is chosen and all of them are reported.

    Args:
        session: An open session.
        managers: The employee's managers in link order (``{"id", "fields"}`` each).
        on_date: The day the approval is routed on (Toronto).

    Returns:
        ``(chosen, skipped)`` where ``chosen`` is the manager to ask or None, and
        ``skipped`` is ``[{"manager_id", "manager", "back"}]`` for each off
        manager passed over (the ``back`` date is an ISO string).
    """
    skipped: list[dict] = []
    for manager in managers:
        back = await off_until(session, manager["id"], on_date)
        if back is None:                                          # the first available manager: stop here
            return manager, skipped
        skipped.append({
            "manager_id": str(manager["id"]),
            "manager": (manager.get("fields") or {}).get("Title", ""),
            "back": back.isoformat(),
        })
    return None, skipped                                          # everyone is off: no one is asked


def _managers_from_maps(employee: dict, by_name: dict[str, dict]) -> list[dict]:
    """The employee's managers in link order, resolved from a prebuilt name map.

    Mirrors ``employee.get_all_managers_for_employee`` (read the ``AllManagers``
    names in order, resolve each by name, drop any that do not resolve) but
    against a staff map built once, so the admin list does not re-read the whole
    directory per manager.

    Args:
        employee: The submitter's staff item (``{"id", "fields"}``).
        by_name: Lower-cased staff name -> staff item.

    Returns:
        The manager staff items in link order.
    """
    managers: list[dict] = []
    for entry in (employee.get("fields", {}).get("AllManagers") or []):
        if isinstance(entry, dict):
            name = (entry.get("LookupValue") or "").strip()
            if name:
                manager = by_name.get(name.lower())
                if manager:
                    managers.append(manager)
    return managers


def _what(record: RequestRecord) -> str:
    """A one-line label of what a request is, for the admin cover list.

    Args:
        record: The pending request row.

    Returns:
        The leave type, the overtime hours, or the carry-over/payout word.
    """
    if record.kind == "leave":
        return record.leave_type or "Leave"
    if record.kind == "overtime":
        return f"{_num(record.hours)} hrs overtime"
    return "Carry Over" if (record.request_option or "") == "carry_over" else "Payout"


def _when(record: RequestRecord) -> str:
    """The dates a request covers, in the house format, for the admin cover list.

    Args:
        record: The pending request row.

    Returns:
        A single date, a date range, or the day count for carry-over/payout
        (which has no event date).
    """
    if record.kind == "carryover_payout":
        return f"{_num(record.days)} days"
    start = record.start_date
    end = record.end_date or start
    if start is None:
        return ""
    if record.kind == "overtime" or start == end:
        return _fmt_date(start)                                   # one day
    return f"{start.strftime('%b')} {start.day} - {_fmt_date(end)}"   # a range


async def list_requests_awaiting_cover() -> list[dict]:
    """Every pending request whose managers are all off today (Toronto) (#370).

    Reads the staff directory once and every pending request, works out each
    request's managers and whether all of them are off today, and returns the
    ones nobody can cover so the admin can approve or reject them. Computed live,
    so a request leaves the list the moment a manager is back, with no stored flag
    to keep in step.

    Returns:
        One dict per stuck request: ``{"request_type", "id", "employee_name",
        "what", "when", "managers": [{"name", "back"}]}``.
    """
    from app.repositories.staff_store import staff_store
    on_date = today_toronto()
    staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)   # one directory read
    by_id = {str(person["id"]): person for person in staff}
    by_name = {(person.get("fields", {}).get("Title") or "").strip().lower(): person for person in staff}
    out: list[dict] = []
    async with async_session() as session:
        pending = (await session.execute(
            select(RequestRecord).where(RequestRecord.status == "pending")
        )).scalars().all()
        # All approved full-day leaves, grouped by the person on leave, read once
        # so each manager's availability is a dict lookup, not a query.
        leave_rows = (await session.execute(
            select(RequestRecord.employee_id, RequestRecord.start_date, RequestRecord.end_date).where(
                RequestRecord.kind == "leave",
                RequestRecord.status == "approved",
                RequestRecord.leave_type != PARTIAL_DAY,
            )
        )).all()
        intervals_by_person: dict[str, list[tuple[date, date | None]]] = {}
        for emp_id, start, end in leave_rows:
            if start is not None:
                intervals_by_person.setdefault(str(emp_id), []).append((start, end))

        for record in pending:
            employee = by_id.get(str(record.employee_id))
            if employee is None:                                  # submitter not matched to a staff row
                continue
            managers = _managers_from_maps(employee, by_name)
            if not managers:                                      # no managers: this is a setup problem, not cover
                continue
            manager_info: list[dict] = []
            all_off = True
            for manager in managers:
                back = _return_date(intervals_by_person.get(str(manager["id"]), []), on_date)
                if back is None:                                  # one is available: nobody needs to cover
                    all_off = False
                    break
                manager_info.append({"name": manager.get("fields", {}).get("Title", ""), "back": _fmt_date(back)})
            if not all_off:
                continue
            out.append({
                "request_type": URL_SEGMENT.get(record.kind, record.kind),
                "id": str(record.item_id),
                "employee_name": record.employee_name or employee.get("fields", {}).get("Title", ""),
                "what": _what(record),
                "when": _when(record),
                "managers": manager_info,
            })
    return out
