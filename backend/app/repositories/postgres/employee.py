"""Staff Directory served from the Postgres ``employees`` table.

Rebuilds the SharePoint list-item shape from an Employee row so the balance
engine, the approval flows and the dashboards read identically whichever
backend is selected. Two parts carry the weight:

**Field translation.** Postgres columns are snake_case (new-code convention);
callers expect SharePoint's column names (``CurrentVacationBalance``,
``CarryOver``, ...). ``_FIELD_TO_COLUMN`` is the single mapping used in both
directions, so a read and a write can never disagree about a name.

**AllManagers synthesis.** In SharePoint an employee's managers live inline on
the record as a multi-value person field; in Postgres they are rows in
``manager_assignments``. This repo rebuilds
``[{"LookupId": ..., "LookupValue": ...}]`` ordered by ``position``, so
``get_all_managers_for_employee`` (which reads ``managers[0]`` as the primary)
keeps working untouched. Unlike Graph — which returns Person/Group fields with
an empty ``LookupValue`` — Postgres knows the manager's name, so both keys are
populated.

Writes deliberately **raise** on a field name they do not recognise rather than
dropping it. A silently ignored balance write would corrupt an employee's
entitlement with no trace; failing loudly at cutover time is far cheaper.
"""
import logging
from datetime import date, datetime

from sqlalchemy import select

from app.database import async_session
from app.models.employee import Employee
from app.models.manager_assignment import ManagerAssignment
from app.repositories.base import EmployeeRepository

logger = logging.getLogger(__name__)

# SharePoint column name -> Employee model attribute.
_FIELD_TO_COLUMN = {
    "Title": "name",
    "EmailAddress": "email",
    "CellNumber": "cell_number",
    "Department": "department",
    "Location": "location",
    "EmployeeType": "employee_type",
    "SalaryHourly": "salary_hourly",
    "CurrentVacationBalance": "vacation_balance",
    "CurrentSickDayBalance": "sick_balance",
    "CurrentOvertimeBalance": "overtime_balance",
    "CarryOver": "carryover_balance",
    "Payout": "payout_balance",
    "DefaultYearlyVacationDays": "vacation_entitlement",
    "SickDayEntitlement": "sick_entitlement",
    "RequestAllowDate": "request_allow_date",
}

_DATE_FIELDS = {"RequestAllowDate"}

# Multi-value person writes arrive as an id list plus an OData type annotation
# (see services/manager_assignments.update_employee_managers). The annotation is
# a Graph protocol detail with no Postgres equivalent.
_MANAGERS_FIELD = "AllManagersLookupId"
_IGNORED_WRITE_FIELDS = {"AllManagersLookupId@odata.type"}


def _parse_date(value):
    """Accept what SharePoint writes: an ISO date or datetime string, or a date."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def _to_sp_shape(row: Employee, assignments: list[ManagerAssignment]) -> dict:
    """Rebuild a SharePoint list item from an Employee row plus its managers.

    ``id`` is ``sp_item_id`` (the Staff Directory item id the app already treats
    as "the employee id"), never the Postgres primary key, so ids captured while
    SharePoint was the source of record keep resolving.
    """
    fields = {}
    for sp_name, column in _FIELD_TO_COLUMN.items():
        value = getattr(row, column)
        if sp_name in _DATE_FIELDS:
            value = value.isoformat() if value else None
        fields[sp_name] = value

    fields["AllManagers"] = [
        {
            "LookupId": a.manager_sp_user_lookup_id,
            "LookupValue": a.manager_name or "",
        }
        for a in sorted(assignments, key=lambda a: a.position)
    ]
    return {"id": row.sp_item_id, "fields": fields}


def _group_assignments(assignments) -> dict[int, list[ManagerAssignment]]:
    grouped: dict[int, list[ManagerAssignment]] = {}
    for a in assignments:
        grouped.setdefault(a.employee_id, []).append(a)
    return grouped


class PostgresEmployeeRepository(EmployeeRepository):
    """Staff Directory backed by Postgres.

    Name and email lookups match in Python rather than SQL, deliberately: the
    comparison is the SharePoint implementation's (case-folded, whitespace
    trimmed) and running the identical predicate is what guarantees the two
    backends resolve the same record. Staff Directory is a few hundred rows, so
    the full read costs nothing next to the Graph round-trip it replaces.
    """

    async def get_all(self) -> list[dict]:
        async with async_session() as session:
            rows = list((await session.execute(select(Employee).order_by(Employee.id))).scalars())
            assignments = list((await session.execute(select(ManagerAssignment))).scalars())

        if not rows:
            logger.warning(
                "STORAGE_EMPLOYEES is 'postgres' but the employees table is empty — "
                "has the employees backfill been run?"
            )
        grouped = _group_assignments(assignments)
        return [_to_sp_shape(row, grouped.get(row.id, [])) for row in rows]

    async def _get_one(self, whereclause) -> dict | None:
        async with async_session() as session:
            row = (await session.execute(select(Employee).where(whereclause))).scalar_one_or_none()
            if row is None:
                return None
            assignments = list(
                (
                    await session.execute(
                        select(ManagerAssignment).where(ManagerAssignment.employee_id == row.id)
                    )
                ).scalars()
            )
        return _to_sp_shape(row, assignments)

    async def get_by_id(self, item_id: str | int) -> dict | None:
        return await self._get_one(Employee.sp_item_id == str(item_id))

    async def get_by_name(self, name: str) -> dict | None:
        # Matches the SharePoint impl's case-insensitive, whitespace-trimmed compare.
        target = (name or "").strip().lower()
        if not target:
            return None
        async with async_session() as session:
            rows = list((await session.execute(select(Employee))).scalars())
            match = next((r for r in rows if (r.name or "").strip().lower() == target), None)
            if match is None:
                return None
            assignments = list(
                (
                    await session.execute(
                        select(ManagerAssignment).where(ManagerAssignment.employee_id == match.id)
                    )
                ).scalars()
            )
        return _to_sp_shape(match, assignments)

    async def get_by_email(self, email: str) -> dict | None:
        target = (email or "").strip().lower()
        if not target:
            return None
        async with async_session() as session:
            rows = list((await session.execute(select(Employee))).scalars())
            match = next((r for r in rows if (r.email or "").strip().lower() == target), None)
            if match is None:
                return None
            assignments = list(
                (
                    await session.execute(
                        select(ManagerAssignment).where(ManagerAssignment.employee_id == match.id)
                    )
                ).scalars()
            )
        return _to_sp_shape(match, assignments)

    async def create(self, fields: dict) -> dict:
        """Insert a new employee row from a SharePoint-shaped field payload.

        Manager assignments are not set here: the caller writes them separately
        via ``update_fields`` (``AllManagersLookupId``), mirroring the SharePoint
        create-then-set-managers flow in ``services/employee_creation``.

        Args:
            fields: SharePoint column names -> values (Title, EmailAddress,
                balances, entitlements), as ``build_employee_fields`` assembles.

        Returns:
            The created employee in ``{"id", "fields"}`` shape.

        Raises:
            KeyError: If a field name is not in ``_FIELD_TO_COLUMN`` — a dropped
                write would silently lose data, so it fails loudly (same rule as
                ``update_fields``).
        """
        values = {}                                        # SP field -> column, translated
        unknown = []                                       # names with no column mapping
        for sp_name, value in fields.items():
            if sp_name in _IGNORED_WRITE_FIELDS or sp_name == _MANAGERS_FIELD:
                continue                                   # managers are written separately, not on the base row
            column = _FIELD_TO_COLUMN.get(sp_name)
            if column is None:
                unknown.append(sp_name)                    # collect, then raise once
                continue
            values[column] = _parse_date(value) if sp_name in _DATE_FIELDS else value
        if unknown:
            raise KeyError(
                f"Cannot write unmapped Staff Directory field(s) {sorted(unknown)} "
                f"to Postgres. Add them to _FIELD_TO_COLUMN (and a migration) "
                f"rather than letting the write be dropped."
            )

        async with async_session() as session:
            # No SharePoint item backs a Postgres-native hire, but the app still
            # treats sp_item_id as "the employee id" (and int()-casts it). Mint
            # the next numeric id above every existing one: unique by construction
            # (strictly greater than all), so it cannot collide with a backfilled
            # SharePoint id. The UNIQUE column is the backstop if two creates race.
            existing = (await session.execute(select(Employee.sp_item_id))).scalars()
            numeric = [int(s) for s in existing if s is not None and str(s).isdigit()]
            new_id = str(max(numeric, default=0) + 1)      # next id above the current max
            session.add(Employee(sp_item_id=new_id, **values))  # insert the row
            await session.commit()                         # persist before the re-read
        return await self.get_by_id(new_id)                # return the {"id","fields"} shape

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Apply a SharePoint-shaped patch to an employee row.

        Unknown field names raise: callers patch balances through here, and a
        silently dropped balance write is unrecoverable data loss.
        """
        async with async_session() as session:
            row = (
                await session.execute(
                    select(Employee).where(Employee.sp_item_id == str(item_id))
                )
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"No employee with sp_item_id {item_id}")

            manager_ids = None
            unknown = []
            for sp_name, value in fields.items():
                if sp_name in _IGNORED_WRITE_FIELDS:
                    continue
                if sp_name == _MANAGERS_FIELD:
                    manager_ids = list(value or [])
                    continue
                column = _FIELD_TO_COLUMN.get(sp_name)
                if column is None:
                    unknown.append(sp_name)
                    continue
                setattr(row, column, _parse_date(value) if sp_name in _DATE_FIELDS else value)

            if unknown:
                raise KeyError(
                    f"Cannot write unmapped Staff Directory field(s) {sorted(unknown)} "
                    f"to Postgres. Add them to _FIELD_TO_COLUMN (and a migration) "
                    f"rather than letting the write be dropped."
                )

            if manager_ids is not None:
                await self._replace_manager_assignments(session, row, manager_ids)

            await session.commit()

        return await self.get_by_id(item_id)

    async def _replace_manager_assignments(self, session, row: Employee, manager_ids: list) -> None:
        """Rewrite this employee's manager rows, preserving the given order.

        Mirrors the SharePoint write, which replaces AllManagers wholesale.
        Names are resolved from the employees table where the manager is also
        an employee; otherwise the row keeps just the lookup id.
        """
        existing = list(
            (
                await session.execute(
                    select(ManagerAssignment).where(ManagerAssignment.employee_id == row.id)
                )
            ).scalars()
        )
        for old in existing:
            await session.delete(old)
        await session.flush()

        if not manager_ids:
            return

        by_lookup_id = {
            e.sp_user_lookup_id: e.name
            for e in (await session.execute(select(Employee))).scalars()
            if e.sp_user_lookup_id is not None
        }
        for position, lookup_id in enumerate(manager_ids):
            session.add(ManagerAssignment(
                employee_id=row.id,
                manager_sp_user_lookup_id=int(lookup_id),
                manager_name=by_lookup_id.get(int(lookup_id)),
                position=position,
            ))
