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
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models.mixins import utcnow
from app.models.staff import Department, Location, Staff, StaffSupervisor
from app.models.staff_change import StaffChange

logger = logging.getLogger(__name__)

# Manager column and the two relation kinds a link row can carry.
ALL_MANAGERS_COLUMN = "AllManagers"
SUPERVISOR_LINK_COLUMN = "SupervisorLink"
RELATION_MANAGER = "manager"             # an AllManagers person-column entry
RELATION_SUPERVISOR = "supervisor_link"  # the single SupervisorLink person
# People added in the app take ids from here up; SharePoint ids stay far below,
# so an app-made id and a SharePoint-made id can never be the same number.
APP_STAFF_ID_START = 1_000_000

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

# SharePoint timestamp columns kept for the shadow sync's newer-wins compare.
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

# The columns a field change is logged against: the fields HR reads in the staff
# history. Links are logged separately (by name), so they are not in this list.
LOGGED_COLUMNS = (
    list(_TEXT_COLUMNS) + list(_NUMBER_COLUMNS) + list(_DATE_COLUMNS) + ["Extension"]
)
# The logged columns whose values are numbers, compared as numbers so 2 and
# "2.0" never read as a change.
NUMBER_COLUMN_SET = set(_NUMBER_COLUMNS) | {"Extension"}


def column_changed(column: str, old, new) -> bool:
    """Whether a logged column's value really moved, comparing numbers as numbers.

    Args:
        column: The SharePoint column name.
        old: The current value (from the rebuilt SharePoint shape).
        new: The incoming value.

    Returns:
        True when the two differ meaningfully.
    """
    if column in NUMBER_COLUMN_SET:                        # numeric compare: 2 == "2.0"
        return _num(old) != _num(new)
    return str(old if old is not None else "").strip() != str(new if new is not None else "").strip()


def diff_logged_fields(before_fields: dict, incoming: dict) -> list[tuple[str, object, object]]:
    """The logged columns present in ``incoming`` whose value changed.

    Shared by the write path (the store's own history) and the shadow sync's
    staff copy, so both log changes against the same set of columns the same way.

    Args:
        before_fields: The person's current SharePoint shape (rebuilt from Postgres).
        incoming: The SharePoint fields being written or read.

    Returns:
        One ``(column, old, new)`` per changed logged column; empty when nothing moved.
    """
    changes: list[tuple[str, object, object]] = []
    for column in LOGGED_COLUMNS:
        if column not in incoming:                         # a partial payload leaves a column alone
            continue
        old = before_fields.get(column)
        new = incoming.get(column)
        if column_changed(column, old, new):
            changes.append((column, old, new))
    return changes


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
        if "@odata.type" in column:
            continue                                      # a Graph type hint, not a column value
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


def _apply_person_links(row: Staff, links: dict) -> None:
    """Replace only the manager/supervisor relations named in ``links``, with names.

    An app write sends the Graph write shape (``AllManagersLookupId`` / a person
    ``*LookupId``), which carries ids but no names, so the names come through this
    ``links`` structure instead. Only the relations present are replaced: a change
    to managers alone leaves the supervisor link untouched, and vice versa.

    Args:
        row: The staff row (its links already loaded, or pending and empty).
        links: ``{"managers": [{"sp_user_id", "name"}, ...]}`` and/or
            ``{"supervisor_link": {"sp_user_id", "name"}}``. A key present replaces
            that relation; an empty managers list clears the managers.
    """
    replace_managers = "managers" in links                # the AllManagers set is being set
    replace_supervisor = "supervisor_link" in links       # the single SupervisorLink is being set
    kept = [                                              # keep the links not being replaced
        link for link in row.supervisors
        if not (link.relation == RELATION_MANAGER and replace_managers)
        and not (link.relation == RELATION_SUPERVISOR and replace_supervisor)
    ]
    new_links = list(kept)                                # start from what survives
    if replace_managers:                                  # add the new approving managers
        for manager in links.get("managers") or []:
            new_links.append(StaffSupervisor(
                relation=RELATION_MANAGER,
                manager_sp_user_id=manager.get("sp_user_id"),
                manager_name=manager.get("name") or "",   # name kept so matching stays by name
            ))
    if replace_supervisor:                                # add the new supervisor link
        supervisor = links.get("supervisor_link")
        if supervisor:
            new_links.append(StaffSupervisor(
                relation=RELATION_SUPERVISOR,
                manager_sp_user_id=supervisor.get("sp_user_id"),
                manager_name=supervisor.get("name") or "",
            ))
    row.supervisors = new_links                           # reassign: orphaned links delete on flush


async def apply_sp_fields(
    session: AsyncSession, row: Staff, fields: dict, links: dict | None = None
) -> list[str]:
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
        links: When given (an app write through the store), the manager/supervisor
            links to set, with names; the person columns in ``fields`` are then
            ignored for link building. When None (a pull or import), the links are
            read from the SharePoint item's person-column blob in ``fields``.

    Returns:
        The SharePoint column names that could not be copied (unmapped columns).
    """
    values, dropped = scalar_updates(fields)              # scalar columns + any unmapped
    for attr, value in values.items():
        setattr(row, attr, value)

    # Replace the links first, while the row may still be pending. An app write
    # passes ``links`` (ids with names); a pull/import passes None and the links
    # come from the person-column blob, but only when the payload names them, so a
    # partial PATCH that names no people leaves the existing links alone.
    if links is not None:
        _apply_person_links(row, links)
    elif ALL_MANAGERS_COLUMN in fields or "SupervisorLinkLookupId" in fields or SUPERVISOR_LINK_COLUMN in fields:
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


async def _carry_rename_to_links(session: AsyncSession, row: Staff, old_name: str) -> None:
    """After a rename, update every manager link that names this person.

    Teams are read by the manager's name (the AllManagers entries, as SharePoint
    stored them), so a renamed manager would otherwise lose their whole team.
    Links already tied to this person's staff row follow the new name; a link
    added after the switch with no staff row yet, but carrying the old name, is
    tied to this row as well. Staff names are unique, so the old name can only
    mean this person.

    Args:
        session: The open session; committed by the caller.
        row: The staff row just written.
        old_name: Its name before the write, trimmed.
    """
    new_name = (row.name or "").strip()
    if not old_name or not new_name or old_name == new_name or row.id is None:
        return                                            # no rename, or a row not yet saved
    await session.execute(
        update(StaffSupervisor)
        .where(or_(
            StaffSupervisor.manager_staff_id == row.id,   # links already tied to this person
            (StaffSupervisor.manager_staff_id.is_(None))
            & (func.lower(func.trim(StaffSupervisor.manager_name)) == old_name.lower()),
        ))
        .values(manager_name=new_name, manager_staff_id=row.id)
        .execution_options(synchronize_session="fetch")    # keep loaded links in step
    )
    logger.info("Staff #%s renamed; manager links now read %r", row.sp_item_id, new_name)


def _manager_names(row: Staff) -> list[str]:
    """The current manager display names on a staff row, for the change history.

    Args:
        row: A staff row with its links loaded.

    Returns:
        The ``manager``-relation names in link order; blank names are kept so the
        before/after comparison lines up with what was stored.
    """
    return [
        (link.manager_name or "")
        for link in sorted(row.supervisors, key=lambda link_row: link_row.id)
        if link.relation == RELATION_MANAGER
    ]


def _write_history_rows(
    before_fields: dict, incoming: dict, links: dict | None, before_managers: list[str]
) -> list[tuple[str, object, object]]:
    """The staff-history rows an app write should record, scalars plus managers.

    Args:
        before_fields: The record's SharePoint shape before the write.
        incoming: The SharePoint fields being written.
        links: The link structure the write carried (see ``apply_sp_fields``), or None.
        before_managers: The manager names before the write (for the AllManagers row).

    Returns:
        One ``(column, old, new)`` per changed logged column, plus one
        ``("AllManagers", before, after)`` row when the manager set changed.
    """
    rows = list(diff_logged_fields(before_fields, incoming))   # scalar columns that moved
    if links and "managers" in links:                          # the manager set was set
        after_names = [(m.get("name") or "") for m in (links.get("managers") or [])]
        before_str = ", ".join(name for name in (before_managers or []) if name)
        after_str = ", ".join(name for name in after_names if name)
        if before_str != after_str:                            # only when it really changed
            rows.append(("AllManagers", before_str or None, after_str or None))
    return rows


async def _announce(item_id) -> None:
    """Tell open dashboards this person changed, after the save committed.

    Args:
        item_id: The person's staff id.
    """
    from app.services.event_bus import announce_staff_change   # lazy: the bus imports the request models
    await announce_staff_change(item_id)


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

    A write (create or update) lands only here: Postgres is the only copy of
    staff, and nothing is written back to SharePoint.
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
        """Add a staff row from a SharePoint-shaped payload.

        A Postgres-native person has no SharePoint item backing them yet, so a
        fresh id is minted in its own range, from APP_STAFF_ID_START up. SharePoint
        hands out its ids from the low end, so an id minted here can never equal
        one SharePoint gave a person before the switch, so two people are never
        merged. Postgres is the only copy: nothing is written back to SharePoint.

        Args:
            list_id: The Staff Directory list id.
            fields: The new person's SharePoint column values.

        Returns:
            The created row as ``{"id","fields"}``.
        """
        async with async_session() as session:
            existing = (await session.execute(select(Staff.sp_item_id))).scalars()
            numeric = [int(s) for s in existing if s is not None and str(s).isdigit()]
            minted = [n for n in numeric if n >= APP_STAFF_ID_START]   # ids this app made before
            new_id = str(max(minted, default=APP_STAFF_ID_START - 1) + 1)   # next in the app's own range
            row = Staff(sp_item_id=new_id)
            session.add(row)
            await apply_sp_fields(session, row, fields)
            row.sp_modified = row.sp_modified or utcnow()  # a fresh row is modified now
            await session.commit()
        await _announce(new_id)                           # open dashboards show the new person
        return await self.get_list_item(list_id, new_id)

    async def update_list_item_fields(
        self, list_id: str, item_id, fields: dict, *,
        links: dict | None = None, history_source: str | None = None,
        history_reason: str | None = None,
    ) -> dict:
        """Merge fields into a staff row and return its fields.

        Args:
            list_id: The Staff Directory list id.
            item_id: The SharePoint item id.
            fields: The SharePoint column values to change.
            links: The manager/supervisor links to set, with names (see
                ``apply_sp_fields``); None when the write touches no person columns.
            history_source: When given, record one ``staff_changes`` row per changed
                column with this source (``"system"`` or ``"admin_dashboard"``), in
                this same transaction. None means the caller records its own history
                (``staff_entitlements`` / the #233 edit path), so the store records
                nothing and the save is never logged twice.
            history_reason: The reason stored on those rows; required when
                ``history_source`` is given.

        Returns:
            The row's SharePoint fields after the change.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such person.
        """
        async with async_session() as session:
            row = await _load_one(session, item_id)
            if row is None:
                raise _not_found(item_id)
            if history_source:                            # capture the "before" for the diff
                before_fields = to_sp_fields(row)         # the record as it stands
                before_managers = _manager_names(row)     # current manager names
                name_now = row.name or ""                 # name at the time of the change
            old_name = (row.name or "").strip()           # to carry a rename onto manager links
            await apply_sp_fields(session, row, fields, links=links)
            row.sp_modified = utcnow()                    # an app edit is modified now
            await _carry_rename_to_links(session, row, old_name)
            if history_source:                            # record history in the same transaction
                for column, old, new in _write_history_rows(
                    before_fields, fields, links, before_managers
                ):
                    session.add(StaffChange(
                        employee_id=str(item_id),
                        employee_name=name_now,
                        field=column,
                        old_value=old,
                        new_value=new,
                        reason=history_reason or "Changed by the system.",
                        source=history_source,
                    ))
            await session.commit()
        await _announce(item_id)                          # open dashboards show the change
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
        await _announce(item_id)                          # open dashboards drop the person


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
