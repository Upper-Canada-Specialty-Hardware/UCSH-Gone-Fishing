"""The three request lists (leave / overtime / carryover-payout) from Postgres.

Unlike employees and holidays, the three request lists have genuinely different
schemas, person-field names, and status columns, so this repository is
configured *per domain*: a field map (SharePoint field name <-> model column),
which of those fields are dates, and which must be stored as strings. One class,
three configs — mirroring how the SharePoint repo is one class instantiated once
per list.

Every method returns/accepts the SharePoint item shape (``{"id", "fields": ...}``),
so the request services, the approval flow, and the balance engine read and write
these dicts unchanged once ``STORAGE_REQUESTS`` flips.

Person and manager references are carried as their ``*LookupId`` fields
(``SubmittedTestLookupId`` for leave, ``SubmittedByLookupId`` for the others,
``ManagerLookupId`` for all) — which is exactly what ``resolve_person_field`` and
the dispatcher already read, so no dict-vs-id reconstruction is needed.
"""
import logging
from datetime import date, datetime

from sqlalchemy import select

from app.database import async_session
from app.models.carryover_payout_request import CarryoverPayoutRequest
from app.models.leave_request import LeaveRequest
from app.models.overtime_request import OvertimeRequest
from app.repositories.base import RequestRepository

logger = logging.getLogger(__name__)


def _parse_date(value):
    """Accept an ISO date/datetime string or a date; return a date or None."""
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


class _Domain:
    """One request list's mapping between SharePoint fields and model columns.

    Attributes:
        model: The SQLAlchemy model for this list.
        field_to_column: SharePoint field name -> model attribute, used in both
            directions so a read and a write can never disagree about a name.
        date_fields: Fields emitted as ISO strings on read and parsed to a date
            on write.
        str_fields: Fields whose value must be stored as a string even when the
            caller passes an int (e.g. EmployeeID into a String column, which
            Postgres would otherwise reject).
    """

    def __init__(self, model, field_to_column: dict, date_fields: set, str_fields: set | None = None):
        self.model = model
        self.field_to_column = field_to_column
        self.date_fields = date_fields
        self.str_fields = str_fields or set()


# Leave: Status + ApproveProcessedFlag; person field is "SubmittedTest".
LEAVE = _Domain(
    LeaveRequest,
    {
        "LeaveType": "leave_type",
        "Status": "status",
        "ApproveProcessedFlag": "approve_processed_flag",
        "StartDate": "start_date",
        "EndDate": "end_date",
        "ApprovedDate": "approved_date",
        "Days": "days",
        "PartialHours": "partial_hours",
        "Title": "title",
        "Notes": "notes",
        "NewBalances": "new_balances",
        "BalanceAuditLog": "balance_audit_log",
        "StaffLocation": "staff_location",
        "StaffDepartment": "staff_department",
        "SubmittedTestLookupId": "submitter_sp_user_lookup_id",
        "ManagerLookupId": "manager_sp_user_lookup_id",
    },
    date_fields={"StartDate", "EndDate", "ApprovedDate"},
)

# Overtime: Status only; person field is "SubmittedBy"; day worked is StartDate.
OVERTIME = _Domain(
    OvertimeRequest,
    {
        "Title": "title",
        "StartDate": "date",
        "Hours": "hours",
        "Status": "status",
        "ApprovedDate": "approved_date",
        "BalanceAuditLog": "balance_audit_log",
        "SubmittedByLookupId": "submitter_sp_user_lookup_id",
        "ManagerLookupId": "manager_sp_user_lookup_id",
    },
    date_fields={"StartDate", "ApprovedDate"},
)

# Carryover/payout: both Status and SystemState; EmployeeID is a stringified id.
CARRYOVER = _Domain(
    CarryoverPayoutRequest,
    {
        "TypeofRequest": "type_of_request",
        "Days": "days",
        "SystemState": "system_state",
        "Status": "status",
        "ApprovedDate": "approved_date",
        "NewBalance": "new_balance",
        "BalanceAuditLog": "balance_audit_log",
        "EmployeeID": "employee_sp_item_id",
        "SubmittedByLookupId": "submitter_sp_user_lookup_id",
        "ManagerLookupId": "manager_sp_user_lookup_id",
    },
    date_fields={"ApprovedDate"},
    str_fields={"EmployeeID"},
)


class PostgresRequestRepository(RequestRepository):
    """A request list backed by Postgres, configured for one domain.

    Instantiated once per list via the factory helpers below, mirroring the
    SharePoint repo's one-instance-per-list shape.
    """

    def __init__(self, domain: _Domain):
        self._d = domain  # the per-list field mapping

    def _to_sp_shape(self, row) -> dict:
        """Rebuild a SharePoint list item from a model row.

        ``id`` is ``sp_item_id`` (the id the app already treats as the request
        id), never the Postgres primary key, so ids captured while SharePoint was
        the source of record keep resolving.
        """
        fields = {}
        for sp_name, column in self._d.field_to_column.items():
            value = getattr(row, column)                         # model value
            if sp_name in self._d.date_fields and value is not None:
                value = value.isoformat()                        # dates -> ISO string, as Graph returns
            fields[sp_name] = value
        return {"id": row.sp_item_id, "fields": fields}

    def _translate(self, fields: dict) -> dict:
        """Translate a SharePoint-shaped patch into model column values.

        Args:
            fields: SharePoint field names -> values.

        Returns:
            Model attribute -> value dict.

        Raises:
            KeyError: On a field name outside this domain's map — a dropped write
                would silently lose data, so it fails loudly.
        """
        values = {}
        unknown = []
        for sp_name, value in fields.items():
            column = self._d.field_to_column.get(sp_name)
            if column is None:
                unknown.append(sp_name)                          # collect, then raise once
                continue
            if sp_name in self._d.date_fields:
                values[column] = _parse_date(value)              # ISO/date -> date
            elif sp_name in self._d.str_fields and value is not None:
                values[column] = str(value)                      # int id -> str (String column)
            else:
                values[column] = value
        if unknown:
            raise KeyError(
                f"Cannot write unmapped request field(s) {sorted(unknown)} to "
                f"Postgres ({self._d.model.__tablename__}). Add them to the field "
                f"map (and a migration) rather than letting the write be dropped."
            )
        return values

    async def get_all(self) -> list[dict]:
        async with async_session() as session:
            rows = list(
                (await session.execute(select(self._d.model).order_by(self._d.model.id))).scalars()
            )
        return [self._to_sp_shape(row) for row in rows]

    async def get_by_id(self, item_id: str | int) -> dict:
        """Return one request, raising if it is absent.

        Mirrors the SharePoint repo, which lets Graph's 404 propagate rather than
        swallowing it — callers (dispatcher, Twilio route) handle their own
        exceptions.

        Raises:
            KeyError: If no request has this ``sp_item_id``.
        """
        async with async_session() as session:
            row = (
                await session.execute(
                    select(self._d.model).where(self._d.model.sp_item_id == str(item_id))
                )
            ).scalar_one_or_none()
        if row is None:
            raise KeyError(f"No request with sp_item_id {item_id}")
        return self._to_sp_shape(row)

    async def create(self, fields: dict) -> dict:
        """Insert a new request row from a SharePoint-shaped payload.

        Mints ``sp_item_id`` as the max existing numeric id + 1 (unique above any
        backfilled SharePoint id; same scheme as the employee/holiday repos).

        Raises:
            KeyError: On an unmapped field name.
        """
        values = self._translate(fields)
        async with async_session() as session:
            existing = (await session.execute(select(self._d.model.sp_item_id))).scalars()
            numeric = [int(s) for s in existing if s is not None and str(s).isdigit()]
            new_id = str(max(numeric, default=0) + 1)            # strictly above the current max
            session.add(self._d.model(sp_item_id=new_id, **values))
            await session.commit()
        return await self.get_by_id(new_id)

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Apply a SharePoint-shaped patch to a request row.

        Raises:
            KeyError: If the request does not exist, or on an unmapped field.
        """
        values = self._translate(fields)
        async with async_session() as session:
            row = (
                await session.execute(
                    select(self._d.model).where(self._d.model.sp_item_id == str(item_id))
                )
            ).scalar_one_or_none()
            if row is None:
                raise KeyError(f"No request with sp_item_id {item_id}")
            for column, value in values.items():
                setattr(row, column, value)                      # apply each translated column
            await session.commit()
        return await self.get_by_id(item_id)


def leave_request_repository() -> PostgresRequestRepository:
    """Postgres repository for the leave requests list."""
    return PostgresRequestRepository(LEAVE)


def overtime_request_repository() -> PostgresRequestRepository:
    """Postgres repository for the overtime requests list."""
    return PostgresRequestRepository(OVERTIME)


def carryover_payout_repository() -> PostgresRequestRepository:
    """Postgres repository for the carryover/payout requests list."""
    return PostgresRequestRepository(CARRYOVER)
