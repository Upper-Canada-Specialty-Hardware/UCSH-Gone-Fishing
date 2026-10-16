"""The Staff Directory served from Postgres, behaving like the SharePoint list.

``PostgresStaffList`` answers the same calls ``sp_client`` answers for the Staff
Directory list (``get_list_items``, ``get_list_item``, ``get_list_item_or_none``,
``create_list_item``, ``update_list_item_fields``, ``delete_list_item``) and
rebuilds the exact SharePoint item shape every caller already consumes:
``{"id": "<sp item id>", "fields": {<SharePoint column>: <value>, ...}}``. So the
balance engine, the approval flow, the dashboards and the exports need no change
when ``staff_storage`` flips from "sharepoint" to "postgres".

The single source for the SharePoint-column mapping lives here, next to the
read-shape rebuild and the upsert the importer uses, so the two directions can
never drift apart. ``id`` is always the SharePoint item id, not the Postgres
primary key, so an id captured while SharePoint was the source of record still
resolves after the switch.
"""
import logging
from datetime import date, datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models.mixins import utcnow
from app.models.staff import Department, Location, Staff, StaffSupervisor

logger = logging.getLogger(__name__)

# Manager column and the two relation kinds a link row can carry.
ALL_MANAGERS_COLUMN = "AllManagers"
SUPERVISOR_LINK_COLUMN = "SupervisorLink"
RELATION_MANAGER = "manager"             # an AllManagers person-column entry
RELATION_SUPERVISOR = "supervisor_link"  # the single SupervisorLink person

# Text/choice columns: SharePoint column -> Staff model attribute. Emitted as ""
# when blank so a caller's ``.get(col, "").strip()`` never meets a None.
_TEXT_COLUMNS = {
    "Title": "name",
    "EmailAddress": "email",
    "CellNumber": "cell_number",
    "Location": "location",          # rebuilt from the lookup relationship
    "Department": "department",      # rebuilt from the lookup relationship
    "SalaryHourly": "salary_hourly",
    "Supervisor": "supervisor_text",
    "SystemCheck": "system_check",
    "Comments": "comments",
}

# Number columns (days): SharePoint column -> Staff model attribute.
_NUMBER_COLUMNS = {
    "DefaultYearlyVacationDays": "vacation_entitlement",
    "SickDayEntitlement": "sick_entitlement",
    "CurrentVacationBalance": "vacation_balance",
    "CurrentSickDayBalance": "sick_balance",
    "CurrentOvertimeBalance": "overtime_balance",
    "CarryOver": "carry_over",
    "Payout": "payout",
}

# Date columns (the day only): SharePoint column -> Staff model attribute.
_DATE_COLUMNS = {
    "Birthday": "birthday",
    "RequestAllowDate": "request_allow_date",
}

# SharePoint timestamp columns kept for the pull's newer-wins compare.
_SP_TIMESTAMP_COLUMNS = {
    "Modified": "sp_modified",
    "Created": "sp_created",
}

# Every SharePoint column this table understands, so the importer can report any
# column it had to drop rather than losing it quietly.
KNOWN_COLUMNS = (
    set(_TEXT_COLUMNS)
    | set(_NUMBER_COLUMNS)
    | set(_DATE_COLUMNS)
    | set(_SP_TIMESTAMP_COLUMNS)
    | {ALL_MANAGERS_COLUMN, "TitleLinkLookupId", "SupervisorLinkLookupId", "id"}
)


def _norm_email(value) -> str | None:
    """Normalise an email for storage and matching: no spaces, lower case.

    Args:
        value: The raw EmailAddress value, or None.

    Returns:
        The address with all whitespace stripped and lower-cased, or None when
        it is blank (stored as NULL so blanks never collide on the unique index).
    """
    cleaned = "".join(str(value or "").split()).lower()   # drop every space, lower case
    return cleaned or None                                # blank -> NULL


def _num(value) -> float | None:
    """A stored number as a float, or None when blank or unreadable.

    Args:
        value: A number column value (may be a number, a digit string, or blank).

    Returns:
        The value as a float, or None.
    """
    if value in (None, ""):                               # blank stays blank
        return None
    try:
        return float(value)                               # numbers may arrive as text
    except (TypeError, ValueError):
        return None                                       # a stray value never crashes


def _int(value) -> int | None:
    """A stored number column as an int, or None when blank or unreadable.

    Args:
        value: An Extension-style number value.

    Returns:
        The value as an int, or None.
    """
    if value in (None, ""):
        return None
    try:
        return int(float(value))                          # "12.0"/"12" -> 12
    except (TypeError, ValueError):
        return None


def _parse_date(value) -> date | None:
    """A date/dateTime column value as a date (day only), or None.

    Args:
        value: An ISO date/datetime string, a date/datetime, or blank.

    Returns:
        The calendar day, or None.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        # Graph emits "...Z"; fromisoformat needs an offset, so swap it in. Slice
        # to the first ten characters first so a bare "YYYY-MM-DD" also parses.
        text = str(value)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        try:
            return date.fromisoformat(str(value)[:10])    # last resort: the day part
        except (ValueError, TypeError):
            return None


def _parse_dt(value) -> datetime | None:
    """A timestamp column value as a tz-aware UTC datetime, or None.

    Args:
        value: An ISO timestamp string (Graph's "...Z"), a datetime, or blank.

    Returns:
        A tz-aware UTC datetime, or None.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _lookup_id(value) -> int | None:
    """The SharePoint user id out of a person column, in either Graph shape.

    Args:
        value: A ``*LookupId`` value, a bare id, or the ``{"LookupId": ...}``
            object Graph sometimes returns.

    Returns:
        The id as an int, or None.
    """
    if isinstance(value, dict):                           # complex object form
        value = value.get("LookupId")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_dt(moment: datetime | None) -> str | None:
    """Format a timestamp the way Graph does ("2026-10-09T14:03:22Z").

    Args:
        moment: An aware (or naive UTC) datetime, or None.

    Returns:
        The UTC timestamp to the second with a trailing Z, or None.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:                             # SQLite hands back naive values
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_sp_fields(row: Staff) -> dict:
    """Rebuild the SharePoint ``fields`` dict from a staff row.

    Text and choice columns come back as "" when blank (so a caller's
    ``.strip()`` is always safe); numbers come back as floats or None; dates and
    timestamps as ISO strings; the managers as the ``AllManagers`` person-column
    blob; the person links as their ``*LookupId`` companions.

    Args:
        row: A Staff row with its location, department and supervisor links
            loaded (selectin on the model loads them with the query).

    Returns:
        The SharePoint-shaped ``fields`` dict for this person.
    """
    fields: dict = {}
    # Text and choice columns, "" when blank.
    fields["Title"] = row.name or ""
    fields["EmailAddress"] = row.email or ""
    fields["CellNumber"] = row.cell_number or ""
    fields["Location"] = (row.location.name if row.location else "") or ""
    fields["Department"] = (row.department.name if row.department else "") or ""
    fields["SalaryHourly"] = row.salary_hourly or ""
    fields["Supervisor"] = row.supervisor_text or ""
    fields["SystemCheck"] = row.system_check or ""
    fields["Comments"] = row.comments or ""
    # Numbers: float or None, which callers read as ``float(x or 0)``.
    fields["DefaultYearlyVacationDays"] = row.vacation_entitlement
    fields["SickDayEntitlement"] = row.sick_entitlement
    fields["CurrentVacationBalance"] = row.vacation_balance
    fields["CurrentSickDayBalance"] = row.sick_balance
    fields["CurrentOvertimeBalance"] = row.overtime_balance
    fields["CarryOver"] = row.carry_over
    fields["Payout"] = row.payout
    # Extension is a number column but int-valued.
    if row.extension is not None:
        fields["Extension"] = row.extension
    # Dates and SharePoint timestamps as ISO strings.
    fields["Birthday"] = row.birthday.isoformat() if row.birthday else None
    fields["RequestAllowDate"] = row.request_allow_date.isoformat() if row.request_allow_date else None
    fields["Modified"] = _iso_dt(row.sp_modified)
    fields["Created"] = _iso_dt(row.sp_created)
    # The person's own Microsoft 365 link, when set.
    if row.title_link_sp_user_id is not None:
        fields["TitleLinkLookupId"] = row.title_link_sp_user_id
    # The approving managers as the AllManagers person-column blob, ordered by
    # link id so the list reads back in the order it was stored.
    managers = []
    supervisor_link_id = None
    for link in sorted(row.supervisors, key=lambda link_row: link_row.id):
        if link.relation == RELATION_MANAGER:
            managers.append({"LookupId": link.manager_sp_user_id, "LookupValue": link.manager_name or ""})
        elif link.relation == RELATION_SUPERVISOR:
            supervisor_link_id = link.manager_sp_user_id
    fields[ALL_MANAGERS_COLUMN] = managers                # always a list, empty when none
    if supervisor_link_id is not None:
        fields["SupervisorLinkLookupId"] = supervisor_link_id
    return fields


def item_shape(row: Staff) -> dict:
    """Build the SharePoint list-item shape from a staff row.

    Args:
        row: A Staff row with relationships loaded.

    Returns:
        ``{"id": <sp item id>, "fields": {...}}`` -- id is the SharePoint item
        id, not the Postgres key.
    """
    return {"id": row.sp_item_id, "fields": to_sp_fields(row)}


def scalar_updates(fields: dict) -> tuple[dict, list[str]]:
    """Translate a SharePoint ``fields`` payload into staff scalar-column values.

    Location, Department and the person/manager links are handled separately
    (they are relationships, not scalars), so they are skipped here.

    Args:
        fields: SharePoint column names -> values.

    Returns:
        A tuple of (model attribute -> value for the scalar columns, a list of
        SharePoint column names this table does not understand so the caller can
        report them as "could not be copied").
    """
    values: dict = {}
    dropped: list[str] = []
    for column, value in fields.items():
        if column in _TEXT_COLUMNS and column not in ("Location", "Department"):
            attr = _TEXT_COLUMNS[column]
            values[attr] = _norm_email(value) if column == "EmailAddress" else (
                None if value in (None, "") else str(value)
            )
        elif column in _NUMBER_COLUMNS:
            values[_NUMBER_COLUMNS[column]] = _num(value)
        elif column in _DATE_COLUMNS:
            values[_DATE_COLUMNS[column]] = _parse_date(value)
        elif column in _SP_TIMESTAMP_COLUMNS:
            values[_SP_TIMESTAMP_COLUMNS[column]] = _parse_dt(value)
        elif column == "Extension":
            values["extension"] = _int(value)
        elif column == "TitleLinkLookupId":
            values["title_link_sp_user_id"] = _lookup_id(value)
        elif column in (ALL_MANAGERS_COLUMN, "SupervisorLinkLookupId", "SupervisorLink", "id",
                        "TitleLink", "AllManagersLookupId"):
            continue                                      # relationships / ids handled elsewhere
        else:
            dropped.append(column)                        # a column the typed table has no home for
    return values, dropped


def manager_links_from_fields(fields: dict) -> list[dict]:
    """The manager and supervisor links a SharePoint payload describes.

    Args:
        fields: SharePoint column names -> values.

    Returns:
        One dict per link: ``{"relation", "sp_user_id", "name"}``. Reads the
        ``AllManagers`` entries and the single ``SupervisorLink``.
    """
    links: list[dict] = []
    managers = fields.get(ALL_MANAGERS_COLUMN)
    if isinstance(managers, list):
        for entry in managers:
            if isinstance(entry, dict):
                uid = _lookup_id(entry.get("LookupId"))
                if uid is not None:
                    links.append({
                        "relation": RELATION_MANAGER,
                        "sp_user_id": uid,
                        "name": entry.get("LookupValue") or "",
                    })
    supervisor = _lookup_id(fields.get("SupervisorLinkLookupId") or fields.get(SUPERVISOR_LINK_COLUMN))
    if supervisor is not None:
        links.append({"relation": RELATION_SUPERVISOR, "sp_user_id": supervisor, "name": ""})
    return links


async def _get_or_create_lookup(session: AsyncSession, model, name):
    """Find a location/department lookup row by name, creating it when new.

    Returns the model instance (not its id) so the caller sets it through the
    relationship and no early flush is needed. A value created earlier in the
    same transaction but not yet flushed is found among the session's pending
    objects, so two people sharing a new location never create it twice.

    Args:
        session: The open session; a new lookup is added, not flushed.
        model: Location or Department.
        name: The choice value as SharePoint stores it.

    Returns:
        The lookup instance, or None when the name is blank.
    """
    cleaned = str(name or "").strip()
    if not cleaned:                                       # blank choice -> no lookup
        return None
    found = (await session.execute(select(model).where(model.name == cleaned))).scalars().first()
    if found is not None:                                 # already in the database
        return found
    for pending in session.new:                           # created earlier this transaction
        if isinstance(pending, model) and pending.name == cleaned:
            return pending
    obj = model(name=cleaned)                             # first time this value is seen
    session.add(obj)
    return obj


async def apply_sp_fields(session: AsyncSession, row: Staff, fields: dict) -> list[str]:
    """Write a SharePoint payload onto a staff row: scalars, links and lookups.

    The row's manager/supervisor links are replaced to match the payload, so a
    manager removed in SharePoint is removed here too. ``manager_staff_id`` is
    left for a later relink pass (it needs every staff row to exist first).

    Nothing is flushed here, and the links are set before the lookups: the row
    may still be pending (a fresh import), and reassigning its link collection
    while pending (or after an eager load, for an update) never fires an async
    lazy load. The caller's commit flushes everything together.

    Args:
        session: The open session; nothing is committed here.
        row: The staff row to write onto (already added to the session). For an
            update it must have been loaded with its links (see ``staff_query``).
        fields: SharePoint column names -> values.

    Returns:
        The SharePoint column names that could not be copied (unmapped columns).
    """
    values, dropped = scalar_updates(fields)              # scalar columns + any unmapped
    for attr, value in values.items():
        setattr(row, attr, value)

    # Replace the links first, while the row may still be pending. Only touch
    # them when the payload carries the person columns, so a partial PATCH that
    # names no people leaves the existing links alone; reassigning the collection
    # orphans the old links, which delete-orphan removes on flush.
    if ALL_MANAGERS_COLUMN in fields or "SupervisorLinkLookupId" in fields or SUPERVISOR_LINK_COLUMN in fields:
        row.supervisors = [
            StaffSupervisor(
                relation=link["relation"],
                manager_sp_user_id=link["sp_user_id"],
                manager_name=link["name"],
            )
            for link in manager_links_from_fields(fields)
        ]

    # Resolve the two choice columns through their lookup tables, set by object
    # so no id (and so no flush) is needed before the row is written.
    if "Location" in fields:
        row.location = await _get_or_create_lookup(session, Location, fields.get("Location"))
    if "Department" in fields:
        row.department = await _get_or_create_lookup(session, Department, fields.get("Department"))
    return dropped


def _not_found(item_id) -> httpx.HTTPStatusError:
    """The 404 Graph raises for a missing item, so callers handle it the same way.

    Args:
        item_id: The missing SharePoint item id.

    Returns:
        An httpx.HTTPStatusError carrying a 404 response.
    """
    request = httpx.Request("GET", f"postgres://staff/items/{item_id}")   # for the message only
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError(f"Staff item {item_id} not found", request=request, response=response)


async def _load_one(session: AsyncSession, sp_item_id) -> Staff | None:
    """One staff row by its SharePoint item id, relationships eagerly loaded.

    Args:
        session: The open session.
        sp_item_id: The SharePoint item id (accepts an int).

    Returns:
        The Staff row, or None.
    """
    return (await session.execute(
        staff_query().where(Staff.sp_item_id == str(sp_item_id))
    )).scalars().first()


class PostgresStaffList:
    """The Staff Directory list in Postgres, with sp_client's list methods.

    A write (create or update) queues a write-back so the change reaches the
    SharePoint list the Bulletin Board and the Microsoft Form still read; a
    read never does.
    """

    async def get_list_items(self, list_id: str, select=None, **kwargs) -> list[dict]:
        """Every staff row in the SharePoint item shape, oldest id first.

        Args:
            list_id: The Staff Directory list id (kept for the same call shape).
            select: When given, only these fields are returned (plus id).
            **kwargs: filter/expand/top are accepted and ignored, as the
                Postgres read returns every row in the one shape callers expect.

        Returns:
            The rows as ``{"id","fields"}`` dicts.
        """
        async with async_session() as session:
            rows = (await session.execute(staff_query())).scalars().all()
            items = [item_shape(row) for row in rows]     # built while the session is open
        if select:                                        # $select keeps only the named columns
            keep = set(select) | {"id"}
            for item in items:
                item["fields"] = {k: v for k, v in item["fields"].items() if k in keep}
        return items

    async def get_list_item(self, list_id: str, item_id) -> dict:
        """One staff row, or the 404 Graph raises when it does not exist.

        Args:
            list_id: The Staff Directory list id.
            item_id: The SharePoint item id (str or int).

        Returns:
            The row as ``{"id","fields"}``.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such person.
        """
        async with async_session() as session:
            row = await _load_one(session, item_id)
            if row is None:
                raise _not_found(item_id)
            return item_shape(row)

    async def get_list_item_or_none(self, list_id: str, item_id) -> dict | None:
        """get_list_item, but a missing person is None instead of an error.

        Args:
            list_id: The Staff Directory list id.
            item_id: The SharePoint item id.

        Returns:
            The row, or None.
        """
        try:
            return await self.get_list_item(list_id, item_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def create_list_item(self, list_id: str, fields: dict) -> dict:
        """Add a staff row from a SharePoint payload and queue the write-back.

        A Postgres-native person has no SharePoint item backing them yet, so a
        fresh sp_item_id is minted above every existing one; the write-back then
        creates the matching SharePoint item in the background.

        Args:
            list_id: The Staff Directory list id.
            fields: The new person's SharePoint column values.

        Returns:
            The created row as ``{"id","fields"}``.
        """
        from app.services.staff_writeback import enqueue_writeback   # avoid an import cycle

        async with async_session() as session:
            existing = (await session.execute(select(Staff.sp_item_id))).scalars()
            numeric = [int(s) for s in existing if s is not None and str(s).isdigit()]
            new_id = str(max(numeric, default=0) + 1)     # strictly above the current max
            row = Staff(sp_item_id=new_id)
            session.add(row)
            await apply_sp_fields(session, row, fields)
            row.sp_modified = row.sp_modified or utcnow()  # a fresh row is modified now
            enqueue_writeback(session, new_id, dict(fields))  # a create is an app change -> replay it
            await session.commit()
        return await self.get_list_item(list_id, new_id)

    async def update_list_item_fields(self, list_id: str, item_id, fields: dict) -> dict:
        """Merge fields into a staff row, queue the write-back, return its fields.

        Args:
            list_id: The Staff Directory list id.
            item_id: The SharePoint item id.
            fields: The SharePoint column values to change.

        Returns:
            The row's SharePoint fields after the change.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such person.
        """
        from app.services.staff_writeback import enqueue_writeback   # avoid an import cycle

        async with async_session() as session:
            row = await _load_one(session, item_id)
            if row is None:
                raise _not_found(item_id)
            await apply_sp_fields(session, row, fields)
            row.sp_modified = utcnow()                    # an app edit is modified now
            enqueue_writeback(session, str(item_id), dict(fields))
            await session.commit()
        return (await self.get_list_item(list_id, item_id))["fields"]

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove a staff row for good (and its links).

        Args:
            list_id: The Staff Directory list id.
            item_id: The SharePoint item id.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such person.
        """
        async with async_session() as session:
            row = await _load_one(session, item_id)
            if row is None:
                raise _not_found(item_id)
            await session.delete(row)
            await session.commit()


def staff_query():
    """A staff select with the location, department and links eagerly loaded.

    The read shape needs all three, so they are selectin-loaded up front; without
    this an async lazy load fires outside the greenlet when the shape is built.

    Returns:
        A SQLAlchemy select over Staff, oldest id first, with eager options.
    """
    return (
        select(Staff)
        .options(
            selectinload(Staff.location),
            selectinload(Staff.department),
            selectinload(Staff.supervisors),
        )
        .order_by(Staff.id)
    )


# The Staff Directory list id, so a caller can check a list id is the staff list.
STAFF_LIST_ID = settings.SP_LIST_STAFF_DIRECTORY
