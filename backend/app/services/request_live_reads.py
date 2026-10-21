"""Indexed reads of the typed ``requests`` table for the dashboards (#330).

When the request lists live in Postgres, the employee, manager and admin
request tables must not fetch a whole list and filter it in memory the way the
SharePoint path does. These helpers run indexed queries instead: an employee
reads only their own rows (the ``employee_id`` index), a manager only their
team's (the ``employee_id`` / ``manager_id`` indexes), an admin all rows
(optionally narrowed by status, on the ``status`` index).

Each row comes back in exactly the Graph list-item shape the SharePoint path
returns, by reusing the typed adapter's ``_item_shape``, with the request's
``stage`` and its typed ``employee_name`` added to the fields. The dashboard
route then runs the same projection it runs for SharePoint items, so the
response shape is identical apart from the extra ``stage``.
"""
import logging
from datetime import date

from sqlalchemy import false, func, or_, select

from app.database import async_session
from app.models.outbox import Outbox
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.outbox_sender import MAX_OUTBOX_ATTEMPTS
from app.services.request_journal import step_key
from app.services.vacation_cover import _fmt_date, off_until, today_toronto

logger = logging.getLogger(__name__)

# The dashboard request_type (url form) for each stored kind, and back again.
WIRE_TO_STORED = {
    "leave": "leave",
    "overtime": "overtime",
    "carryover-payout": "carryover_payout",
}


def _parse_date(value: str | None) -> date | None:
    """Read an ISO date query value, or None when it is empty or unparseable.

    Args:
        value: "YYYY-MM-DD", or None.

    Returns:
        The date, or None.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])                  # tolerate a trailing time part
    except ValueError:
        logger.warning("Ignoring an unparseable request-filter date: %r", value)
        return None


async def _derived_pending_stage(session, record: RequestRecord, keys_by_request: dict,
                                 today: date, off_cache: dict) -> str | None:
    """The stage to show for one pending request, from its state rather than its last step (#423).

    A pending request's stored stage is its last journal step (e.g. "Submitted,
    receipt sent"), which goes stale while it waits on a manager. This recomputes
    it: "Waiting for <manager>" once a manager was asked, or "Waiting, <manager>
    away until <date>" when the assigned manager is off and nobody was asked
    (reusing the vacation routing). Returns None to keep the stored stage.

    Args:
        session: The open read session (for the manager's away lookup).
        record: The pending request row.
        keys_by_request: request.id -> its manager_approval outbox keys.
        today: Today in Toronto, for the away check.
        off_cache: manager_id -> their ``off_until`` result, filled as managers
            are seen, so one read runs at most one away query per manager (finding 10).

    Returns:
        The derived stage line, or None to fall back to the stored stage.
    """
    name = record.manager_name or ""
    # Asked once a manager_approval row exists for the current version; an imported
    # row was asked by the old path, so it counts as asked (aligned with
    # request_submission._manager_ever_asked).
    prefix = step_key(record.kind, record.item_id, "sent_to_manager", record.approval_version)
    asked = record.sp_item_id is not None or any(
        key.startswith(f"{prefix}:") for key in keys_by_request.get(record.id, [])
    )
    if asked and name:
        return f"Waiting for {name}"                               # it is with a manager now
    if not asked and record.manager_id:
        # Never asked and the assigned manager is away: say who and until when.
        # Cache per manager: several pending requests often share one manager, so
        # without this the same away query ran once per never-asked row (finding 10).
        if record.manager_id not in off_cache:
            off_cache[record.manager_id] = await off_until(session, record.manager_id, today)
        back = off_cache[record.manager_id]
        if back is not None and name:
            return f"Waiting, {name} away until {_fmt_date(back)}"
    return None                                                   # otherwise keep the stored label


async def indexed_request_items(
    request_type: str,
    *,
    employee_id: str | None = None,
    team_employee_ids: set[str] | None = None,
    manager_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    with_send_status: bool = False,
) -> list[dict]:
    """Rows of one request kind, filtered by an indexed scope, in Graph shape.

    The scope is chosen by which arguments are given:
    - ``employee_id`` only: that employee's own rows (employee view).
    - ``team_employee_ids`` and/or ``manager_id``: the manager's team (a row is
      theirs when its employee is on the team, or its manager_id is the manager).
    - none of the three: every row of the kind (admin view).

    Args:
        request_type: The url-form kind ("leave", "overtime", "carryover-payout").
        employee_id: Restrict to this employee's Staff Directory id.
        team_employee_ids: The team's employee ids (manager view).
        manager_id: The manager's own id (manager view).
        status: Keep only this status (title or lower case); None for every status.
        from_date: Keep rows whose start_date is on or after this ISO date.
        to_date: Keep rows whose start_date is on or before this ISO date.
        with_send_status: True also adds ``failed_send`` to each row's fields, True
            when the request has an outbox message that finally failed (#424); the
            admin requests table passes it, the employee/manager views do not.

    Returns:
        A list of ``{"id", "createdDateTime", "lastModifiedDateTime", "fields"}``
        items, each with ``stage`` and ``employee_name`` added to ``fields``. A
        pending row's ``stage`` is derived from its state (#423), not its last
        journal step, so it does not go stale.
    """
    stored_kind = WIRE_TO_STORED[request_type]                  # kind as stored in the table
    adapter = TypedRequestList(stored_kind)                     # reused only for its shape conversion
    conditions = [RequestRecord.kind == stored_kind]           # always scoped to one kind

    manager_scope = team_employee_ids is not None or manager_id is not None
    if employee_id is not None:
        conditions.append(RequestRecord.employee_id == str(employee_id))   # own rows (employee view)
    elif manager_scope:
        team = {str(x) for x in (team_employee_ids or set())}  # team member ids as strings
        branches = []
        if manager_id:
            branches.append(RequestRecord.manager_id == str(manager_id))   # rows routed to this manager
        if team:
            branches.append(RequestRecord.employee_id.in_(team))           # rows of the manager's people
        conditions.append(or_(*branches) if branches else false())          # empty team: nothing matches

    if status:
        conditions.append(func.lower(RequestRecord.status) == status.strip().lower())
    start_from = _parse_date(from_date)
    start_to = _parse_date(to_date)
    if start_from is not None:
        # Null start_date (carry-over/payout) passes, matching the SharePoint filter.
        conditions.append(or_(RequestRecord.start_date.is_(None), RequestRecord.start_date >= start_from))
    if start_to is not None:
        conditions.append(or_(RequestRecord.start_date.is_(None), RequestRecord.start_date <= start_to))

    async with async_session() as session:
        rows = (await session.execute(
            select(RequestRecord)
            .where(*conditions)
            .order_by(RequestRecord.created_at, RequestRecord.item_id)
        )).scalars().all()

        # Derive each pending row's live stage (#423) and, for the admin table,
        # mark rows whose send finally failed (#424). Both run only when there is
        # something to compute, so an all-decided or empty read pays nothing.
        derived_stage: dict[int, str] = {}                     # row.id -> live stage, pending only
        failed_ids: set[int] = set()
        pending_rows = [row for row in rows if row.status == "pending"]
        if pending_rows:
            # One bulk query for the manager_approval rows of every pending request.
            keys_by_request: dict[int, list[str]] = {}
            for rid, key in (await session.execute(
                select(Outbox.request_id, Outbox.idempotency_key).where(
                    Outbox.request_id.in_([row.id for row in pending_rows]),
                    Outbox.message_type == "manager_approval",
                )
            )).all():
                keys_by_request.setdefault(rid, []).append(key)
            today = today_toronto()                            # Toronto day, matching the routing (#369)
            off_cache: dict = {}                               # manager_id -> off_until, one lookup per manager (finding 10)
            for row in pending_rows:
                stage = await _derived_pending_stage(session, row, keys_by_request, today, off_cache)
                if stage is not None:
                    derived_stage[row.id] = stage
        if with_send_status:
            # A message has finally failed when it is "failed" with no further
            # retry scheduled (permanent) or stuck at the attempt cap (#419, #424).
            failed_ids = {
                rid for (rid,) in (await session.execute(
                    select(Outbox.request_id).where(
                        Outbox.request_id.in_([row.id for row in rows]),
                        Outbox.status == "failed",
                        or_(Outbox.next_attempt_at.is_(None), Outbox.attempts >= MAX_OUTBOX_ATTEMPTS),
                    )
                )).all()
            }

    items = []
    for row in rows:
        item = adapter._item_shape(row)                        # the exact SharePoint-shaped item
        fields = item["fields"]
        # A pending row shows its live stage; a decided row keeps its stored one (#423).
        fields["stage"] = derived_stage.get(row.id) or row.stage or ""
        fields["employee_name"] = row.employee_name or ""      # typed name, used when no person blob exists
        fields["employment_type"] = row.employment_type or ""  # type recorded on the request, wins over today's (#338)
        if with_send_status:
            fields["failed_send"] = row.id in failed_ids       # the admin table's failed-send chip (#424)
        if row.manager_id and "ManagerLookupId" not in fields:
            # Mark a routed request processed so the pending-visibility check passes.
            fields["ManagerLookupId"] = row.manager_id
        items.append(item)
    return items
