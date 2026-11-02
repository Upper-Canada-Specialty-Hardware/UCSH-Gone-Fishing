"""The typed ``requests`` table dressed up as a SharePoint request list (#321).

``TypedRequestList`` answers the same calls ``PostgresRequestList`` answers and
returns the same Graph list-item shapes, so the request services cannot tell
which one is behind ``request_store``. The difference is underneath: instead of
one JSON blob per item, it reads and writes real columns on ``RequestRecord``,
translating between SharePoint column names and typed columns through one
mapping (``SIMPLE_MAP`` plus the handful of columns that need converting). Any
column it does not recognise is kept verbatim in ``extra``, so nothing
SharePoint carried is lost.

One instance serves one kind ("leave", "overtime" or "carryover_payout"); all
three share the ``requests`` table, told apart by ``kind``. A new item draws its
``item_id`` from ``request_counters`` (the number people and links use), and the
Graph ``"id"`` returned is ``str(item_id)``. Every field write also records a
``request_changes`` row, so the history of a request is kept as it moves.

Leave Titles carry the employee's name or a description depending on the intake
path; this adapter splits an incoming Title with ``split_leave_title`` and
rebuilds it on read with ``compose_leave_title`` (the descriptions bug, #321).
"""
import json
import logging
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import func, select

from app.database import async_session
from app.models.mixins import utcnow
from app.models.request_record import RequestRecord
from app.repositories.postgres.requests import _iso, _not_found, _stored, _to_int
from app.services.request_descriptions import compose_leave_title, split_leave_title
from app.services.request_journal import next_item_id, record_change
from app.services.request_states import INITIAL_STATE, STATUS_TO_STATE

logger = logging.getLogger(__name__)

# SharePoint column -> RequestRecord attribute, for the columns that map straight
# across. The columns that need converting (Status, dates, TypeofRequest, Title)
# are handled on their own below; every other column is kept in ``extra``.
SIMPLE_MAP = {
    "LeaveType": "leave_type",
    "EndDate": "end_date",             # a date
    "Days": "days",                    # a number
    "Hours": "hours",                  # a number (overtime)
    "EmployeeID": "employee_id",
    "ManagerID": "manager_id",
    "SubmitterEmail": "employee_email",
    "StaffDepartment": "department",
    "StaffLocation": "location",
}
# SharePoint "Carry Over" / "Payout" <-> stored request_option.
OPTION_TO_STORED = {"carry over": "carry_over", "payout": "payout"}
OPTION_TO_WIRE = {"carry_over": "Carry Over", "payout": "Payout"}


def choice_text(value) -> str | None:
    """The plain name in a department or location value, however it arrived.

    The Power Automate flow sometimes writes these columns as a SharePoint lookup
    object, either as a dict or as its JSON text
    ('{"@odata.type": "...SPListExpandedReference", "Id": 5, "Value": "Hardware"}').
    Only the ``Value`` is the name; anything else is kept as given.

    Args:
        value: The SharePoint value: a plain string, a lookup dict, its JSON
            text, or None.

    Returns:
        The name, or None when the value is blank.
    """
    if isinstance(value, dict):                                  # the lookup object itself
        value = value.get("Value")                               # its name
    elif isinstance(value, str) and value.lstrip().startswith("{"):   # the object as JSON text
        try:
            parsed = json.loads(value)                           # read it back
        except ValueError:
            parsed = None                                        # not JSON after all: keep the text
        if isinstance(parsed, dict) and "Value" in parsed:
            value = parsed["Value"]                              # its name
    text = str(value).strip() if value is not None else ""      # tidy the result
    return text or None                                          # blank stays empty


def _to_date(value) -> date | None:
    """Parse a SharePoint date value into a date.

    Args:
        value: An ISO date ("2026-11-02") or datetime string, a date, or None.

    Returns:
        The date, or None when the value is empty or unparseable.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).replace("Z", "+00:00")                    # Graph uses a trailing Z
    try:
        return datetime.fromisoformat(text).date() if "T" in text else date.fromisoformat(text)
    except ValueError:
        logger.warning("Ignoring an unparseable request date: %r", value)
        return None


def _to_dt(value) -> datetime | None:
    """Parse a SharePoint date/datetime value into an aware UTC datetime.

    Args:
        value: An ISO string, a datetime, a date, or None.

    Returns:
        An aware UTC datetime, or None when empty or unparseable.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text) if "T" in text else datetime.fromisoformat(f"{text}T00:00:00")
    except ValueError:
        logger.warning("Ignoring an unparseable request timestamp: %r", value)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _to_float(value) -> float | None:
    """Coerce a numeric value to a float, or None when it cannot be one.

    Args:
        value: A number or numeric string, or None.

    Returns:
        The float, or None.
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TypedRequestList:
    """One request kind, served from the typed ``requests`` table.

    Args:
        kind: The request kind this instance serves (one of request_vocab.KINDS).
    """

    def __init__(self, kind: str):
        self.kind = kind

    # ----- reads -----

    async def get_list_items(
        self,
        list_id: str,
        filter: str | None = None,
        select: list[str] | None = None,
        expand: str = "fields",
        top: int = 5000,
    ) -> list[dict]:
        """Every item of this kind, oldest first, like an unfiltered Graph read.

        Args:
            list_id: The SharePoint list id (kept for the same call shape).
            filter: Not supported; no request-list read uses an OData filter.
            select: When given, only these fields are returned (plus id).
            expand: Ignored; fields are always included.
            top: Ignored; everything is returned.

        Returns:
            The items in the Graph list-item shape.

        Raises:
            NotImplementedError: If an OData filter is passed.
        """
        if filter:
            raise NotImplementedError("OData filters are not supported on the Postgres request lists")
        async with async_session() as session:
            rows = (await session.execute(
                sa_select_kind(self.kind)
            )).scalars().all()
        items = [self._item_shape(row) for row in rows]
        if select:                                              # $select keeps only the named columns
            keep = set(select) | {"id"}
            for item in items:
                item["fields"] = {k: v for k, v in item["fields"].items() if k in keep}
        return items

    async def get_list_item(self, list_id: str, item_id) -> dict:
        """One item, or the 404 error Graph raises when it does not exist.

        Args:
            list_id: The SharePoint list id.
            item_id: The item_id (str or int).

        Returns:
            The item in the Graph list-item shape.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        async with async_session() as session:
            row = await self._row(session, item_id)
        if row is None:
            raise _not_found(list_id, item_id)
        return self._item_shape(row)

    async def get_list_item_or_none(self, list_id: str, item_id) -> dict | None:
        """get_list_item, but a missing item is None instead of an error.

        Args:
            list_id: The SharePoint list id.
            item_id: The item_id.

        Returns:
            The item, or None.
        """
        try:
            return await self.get_list_item(list_id, item_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def find_by_sp_item_id(self, sp_item_id: str) -> dict | None:
        """The row moved from a given SharePoint item, if there is one.

        Args:
            sp_item_id: The SharePoint item id.

        Returns:
            The item in the Graph list-item shape, or None.
        """
        async with async_session() as session:
            row = (await session.execute(
                select(RequestRecord)
                .where(RequestRecord.kind == self.kind, RequestRecord.sp_item_id == str(sp_item_id))
            )).scalars().first()
        return self._item_shape(row) if row else None

    async def sp_item_ids(self) -> set[str]:
        """The SharePoint item ids already moved into this kind.

        Returns:
            A set of SharePoint item ids, as strings.
        """
        async with async_session() as session:
            rows = (await session.execute(
                select(RequestRecord.sp_item_id)
                .where(RequestRecord.kind == self.kind, RequestRecord.sp_item_id.is_not(None))
            )).all()
        return {r[0] for r in rows}

    async def count(self) -> int:
        """How many items of this kind the table holds.

        Returns:
            The row count.
        """
        async with async_session() as session:
            return int((await session.execute(
                select(func.count()).select_from(RequestRecord).where(RequestRecord.kind == self.kind)
            )).scalar_one())

    # ----- writes -----

    async def create_list_item(
        self,
        list_id: str,
        fields: dict,
        *,
        item_id: int | None = None,
        sp_item_id: str | None = None,
        created_at: datetime | None = None,
        modified_at: datetime | None = None,
        actor: str = "system",
        channel: str = "system",
    ) -> dict:
        """Add an item, as Graph's POST .../items does.

        Args:
            list_id: The SharePoint list id.
            fields: The new item's column values, in SharePoint column names.
            item_id: Keep this item_id (importing); None takes the next per kind.
            sp_item_id: The SharePoint item this row came from, if any.
            created_at: The original creation time (importing); None means now.
            modified_at: The original modification time; None means now.
            actor: Who made it, recorded on each field's change row.
            channel: Where it came from, recorded on each field's change row.

        Returns:
            The new item in the Graph list-item shape.
        """
        stored = _stored(fields)                                # drop id/Created/Modified
        now = utcnow()
        async with async_session() as session:
            if item_id is None:
                item_id = await next_item_id(session, self.kind)   # the next number for this kind
            else:
                await self._raise_counter(session, item_id)        # kept item (copy/import): counter stays ahead
            record = RequestRecord(kind=self.kind, item_id=item_id, sp_item_id=sp_item_id)
            record.created_at = created_at or now
            record.updated_at = modified_at or created_at or now
            await self._resolve_people(record, stored)          # name/email first, so the Title split sees them
            self._apply_fields(record, stored)
            # The state-machine position a brand-new row starts in (#455): a decided
            # status maps straight across (an imported Approved/Rejected row keeps its
            # state), and a fresh pending request begins getting_ready until submit
            # moves it on. The importer overrides this with its fuller signal set.
            record.state = STATUS_TO_STATE.get(record.status, INITIAL_STATE)
            session.add(record)
            await session.flush()                               # assign id and enforce the CHECK constraints
            for col, value in stored.items():                   # a field write is a change, even on create
                record_change(session, record, col, None, value, actor, channel, "created")
            await session.commit()
            await session.refresh(record)
        return self._item_shape(record)

    async def update_list_item_fields(
        self, list_id: str, item_id, fields: dict, *, actor: str = "system", channel: str = "system"
    ) -> dict:
        """Merge fields into an item, as Graph's PATCH .../fields does.

        Fields not named are left as they are. Each named field also writes a
        request_changes row recording the old and new value.

        Args:
            list_id: The SharePoint list id.
            item_id: The item_id.
            fields: The column values to change, in SharePoint column names.
            actor: Who changed them.
            channel: Where the change came from.

        Returns:
            The item's fields after the change (what Graph's PATCH returns).

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        stored = _stored(fields)
        async with async_session() as session:
            record = await self._row(session, item_id)
            if record is None:
                raise _not_found(list_id, item_id)
            before = self._item_shape(record)["fields"]         # the SharePoint-shaped values before
            await self._resolve_people(record, stored)          # fill any newly-named person before the Title split
            self._apply_fields(record, stored)
            record.updated_at = utcnow()
            await session.flush()                               # enforce CHECKs before recording changes
            after = self._item_shape(record)["fields"]
            for col in stored:                                  # one change row per field written
                record_change(session, record, col, before.get(col), after.get(col), actor, channel, "updated")
            await session.commit()
            await session.refresh(record)
        return self._item_shape(record)["fields"]

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove an item for good, as Graph's DELETE does.

        Args:
            list_id: The SharePoint list id.
            item_id: The item_id.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        async with async_session() as session:
            record = await self._row(session, item_id)
            if record is None:
                raise _not_found(list_id, item_id)
            await session.delete(record)
            await session.commit()

    # ----- translation -----

    async def _raise_counter(self, session, item_id: int) -> None:
        """Keep this kind's id counter at or above a kept item_id (copy/import).

        A copied or imported item keeps its own item_id instead of drawing the
        next number, so the counter would still hand that number out to a new
        request and clash. This lifts the counter to at least ``item_id`` (the
        importer's "raise the counter above the largest imported id" rule).

        Args:
            session: An open session; not committed here.
            item_id: The kept item_id this row is stored under.
        """
        from app.models.request_counter import RequestCounter       # local import: keep the module's top light
        counter = await session.get(RequestCounter, self.kind)
        if counter is None:                                         # first kept item of this kind
            session.add(RequestCounter(kind=self.kind, last_item_id=item_id))
        elif counter.last_item_id < item_id:                        # a higher id than seen so far
            counter.last_item_id = item_id

    async def _row(self, session, item_id) -> RequestRecord | None:
        """The record for an item_id of this kind, or None.

        Args:
            session: An open session.
            item_id: The item_id (str or int).

        Returns:
            The RequestRecord, or None when the id is not a number or not found.
        """
        key = _to_int(item_id)
        if key is None:
            return None
        return (await session.execute(
            select(RequestRecord).where(RequestRecord.kind == self.kind, RequestRecord.item_id == key)
        )).scalars().first()

    async def _resolve_people(self, record: RequestRecord, stored: dict) -> None:
        """Fill the employee's and manager's names (and the employee's email) from staff.

        The leave Title split and the result email both need the employee name,
        and app-made requests arrive with an id but no name. The name is looked
        up from the Staff Directory through the employee service, keyed by the
        EmployeeID / ManagerID the caller passed. It runs when staff live in
        Postgres (a table read, no network; always true after the switch, when
        the SharePoint site is never resolved), or before the switch when
        SharePoint is connected (``sp_client.site_id`` set). Tests that keep
        staff in SharePoint have no site id, so they make no network call. An
        explicit SubmitterEmail always wins over the looked-up address.

        Args:
            record: The record being written; its name fields are filled in place.
            stored: The incoming SharePoint-named fields (system fields removed).
        """
        from app.graph.sharepoint import sp_client                 # local import: avoid a load-time cycle
        from app.repositories.staff_store import staff_in_postgres  # local import: avoid a load-time cycle
        if not await staff_in_postgres() and getattr(sp_client, "site_id", None) is None:
            return                                                 # staff in SharePoint and no site: skip, no call
        from app.services.employee import get_employee_by_id       # the existing Staff Directory read
        emp_id = stored.get("EmployeeID") if "EmployeeID" in stored else record.employee_id
        if emp_id and not record.employee_name:                    # only when we still lack the name
            employee = await get_employee_by_id(emp_id)            # None when the id is unknown or SP is down
            if employee:
                emp_fields = employee.get("fields", {})
                record.employee_name = emp_fields.get("Title") or record.employee_name  # their name at the time
                if not stored.get("SubmitterEmail") and not record.employee_email:       # SubmitterEmail wins
                    record.employee_email = (emp_fields.get("EmailAddress") or "").strip().lower() or None
        mgr_id = stored.get("ManagerID") if "ManagerID" in stored else record.manager_id
        if mgr_id and not record.manager_name:                     # name the deciding manager when we can
            manager = await get_employee_by_id(mgr_id)
            if manager:
                record.manager_name = manager.get("fields", {}).get("Title") or record.manager_name

    def _apply_fields(self, record: RequestRecord, stored: dict) -> None:
        """Write SharePoint-named fields onto a record's typed columns.

        Title is applied last, after LeaveType, so the leave split sees the
        right leave type regardless of field order.

        Args:
            record: The record to write onto.
            stored: Fields in SharePoint column names (system fields removed).
        """
        extra = dict(record.extra or {})                        # keep anything already parked in extra
        title = None
        for col, value in stored.items():
            if col == "Title":
                title = value                                   # handled after the loop
            elif col in SIMPLE_MAP:
                self._set_simple(record, SIMPLE_MAP[col], value)
            elif col in ("StartDate", "Date"):                  # both lists use StartDate; "Date" accepted for older rows
                record.start_date = _to_date(value)
            elif col == "Status":
                if value:
                    record.status = str(value).strip().lower()  # stored lower case
            elif col == "TypeofRequest":
                record.request_option = OPTION_TO_STORED.get(str(value).strip().lower()) if value else None
            elif col == "ApprovedDate":
                record.decided_at = _to_dt(value)
            else:
                extra[col] = value                              # unknown column: kept verbatim
        record.extra = extra                                    # a new dict so the JSON change is saved
        if title is not None:
            self._apply_title(record, title)

    def _set_simple(self, record: RequestRecord, attr: str, value) -> None:
        """Set one plainly-mapped column, converting dates, numbers and email.

        Args:
            record: The record to write onto.
            attr: The RequestRecord attribute name.
            value: The SharePoint value.
        """
        if attr == "end_date":
            record.end_date = _to_date(value)
        elif attr in ("days", "hours"):
            setattr(record, attr, _to_float(value))
        elif attr == "employee_email":
            record.employee_email = (str(value).strip().lower() or None) if value else None
        elif attr == "employee_id":
            record.employee_id = str(value) if value is not None else None
        elif attr == "manager_id":
            record.manager_id = str(value) if value else None
        elif attr in ("department", "location"):
            setattr(record, attr, choice_text(value))           # a lookup object stores its name only
        else:
            setattr(record, attr, value)

    def _apply_title(self, record: RequestRecord, title) -> None:
        """Store an incoming Title into the right typed columns per kind.

        Args:
            record: The record to write onto.
            title: The raw Title value.
        """
        if self.kind == "leave":
            # Split into description + relation; needs the (now current) leave type.
            description, relation = split_leave_title(title, record.employee_name, record.leave_type)
            record.description = description or None
            record.bereavement_relation = relation
        elif self.kind == "overtime":
            record.description = (title or None)                # overtime Title is the description as-is
        # carry-over/payout has no description, so a Title is ignored.

    def _compose_title(self, record: RequestRecord) -> str | None:
        """Rebuild the SharePoint Title from a record's typed columns.

        Args:
            record: The record to read.

        Returns:
            The Title string, or None when this kind carries none.
        """
        if self.kind == "leave":
            # Rebuild so code calling extract_request_description shows the text.
            return compose_leave_title(record.employee_name or "", record.description or "")
        if self.kind == "overtime":
            return record.description or None
        return None

    def _item_shape(self, record: RequestRecord) -> dict:
        """Build the Graph list-item shape from a record.

        Args:
            record: The record to render.

        Returns:
            {"id", "createdDateTime", "lastModifiedDateTime", "fields"}; the
            Graph "id" is str(item_id), and fields carries the SharePoint column
            names the services expect plus id/Created/Modified.
        """
        fields = dict(record.extra or {})                       # unknown columns first, typed columns override
        if record.status:
            fields["Status"] = record.status.capitalize()      # title case on the wire
        # The state-machine position (#455), snake_case, so the dashboards render
        # it. Absent on the SharePoint path, where the frontend keeps today's look.
        fields["state"] = record.state
        if record.leave_type is not None:
            fields["LeaveType"] = record.leave_type
        if record.start_date is not None:
            # Leave and overtime both read their date from StartDate (the column
            # the overtime form writes and every overtime reader uses).
            fields["StartDate"] = record.start_date.isoformat()
        if record.end_date is not None:
            fields["EndDate"] = record.end_date.isoformat()
        if record.days is not None:
            fields["Days"] = record.days
        if record.hours is not None:
            fields["Hours"] = record.hours
        if record.request_option is not None:
            fields["TypeofRequest"] = OPTION_TO_WIRE.get(record.request_option, record.request_option)
        if record.decided_at is not None:
            fields["ApprovedDate"] = _iso(record.decided_at)
        if record.employee_id is not None:
            fields["EmployeeID"] = record.employee_id
        if record.manager_id is not None:
            fields["ManagerID"] = record.manager_id
        if record.employment_type is not None:
            # How the person was paid when the request applied. The balance
            # display reads this instead of the person's current SalaryHourly, so
            # an approved request's pots and the hourly vacation counter stay
            # right even if the person's type later changed (#300). Read-only: it
            # is never written back, so it never lands in a change row.
            fields["RequestEmploymentType"] = record.employment_type
        if record.employee_email is not None:
            fields["SubmitterEmail"] = record.employee_email
        if record.department is not None:
            fields["StaffDepartment"] = record.department
        if record.location is not None:
            fields["StaffLocation"] = record.location
        title = self._compose_title(record)
        if title:
            fields["Title"] = title
        created = _iso(record.created_at)
        modified = _iso(record.updated_at)
        fields.update({"id": str(record.item_id), "Created": created, "Modified": modified})
        return {
            "id": str(record.item_id),
            "createdDateTime": created,
            "lastModifiedDateTime": modified,
            "fields": fields,
        }


def sa_select_kind(kind: str):
    """Every row of one kind, oldest item_id first.

    Args:
        kind: The request kind.

    Returns:
        A SQLAlchemy select statement.
    """
    return select(RequestRecord).where(RequestRecord.kind == kind).order_by(RequestRecord.item_id)
