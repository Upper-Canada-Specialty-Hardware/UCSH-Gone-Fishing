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
from app.models.request_record import RequestRecord
from app.repositories.postgres.typed_requests import TypedRequestList

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


async def indexed_request_items(
    request_type: str,
    *,
    employee_id: str | None = None,
    team_employee_ids: set[str] | None = None,
    manager_id: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
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

    Returns:
        A list of ``{"id", "createdDateTime", "lastModifiedDateTime", "fields"}``
        items, each with ``stage`` and ``employee_name`` added to ``fields``.
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

    items = []
    for row in rows:
        item = adapter._item_shape(row)                        # the exact SharePoint-shaped item
        fields = item["fields"]
        fields["stage"] = row.stage or ""                      # the new stage column (Postgres only)
        fields["employee_name"] = row.employee_name or ""      # typed name, used when no person blob exists
        fields["employment_type"] = row.employment_type or ""  # type recorded on the request, wins over today's (#338)
        if row.manager_id and "ManagerLookupId" not in fields:
            # Mark a routed request processed so the pending-visibility check passes.
            fields["ManagerLookupId"] = row.manager_id
        items.append(item)
    return items
