"""Edit a person's entitlements and balances from the admin dashboard (#272).

HR opens the admin person panel, changes any of the editable Staff Directory
figures (the two yearly entitlements and the five balance pots), gives a reason,
and saves. Every save that actually changes a value is written to SharePoint and
recorded in the change history (``staff_changes``) so the figures are auditable.

The write path mirrors the existing ``/admin/set-balances`` endpoint: it is
gated on ``PROCESSING_ENABLED`` at the route, runs under the per-employee lock so
a concurrent save cannot interleave a read-modify-write, re-reads the record
inside the lock, writes only the columns that differ, and recomputes the
request-allow date when vacation or carry-over moved.

The pure planning step (``plan_changes``) does all validation without any I/O so
it can be unit-tested on its own; the async wrapper adds the lock, the reads and
writes, and the history call.
"""

import logging
import math

from app.config import settings
from app.repositories.staff_store import staff_in_postgres, staff_store
from app.services import staff_changes
from app.services.balance import recalculate_request_allow_date
from app.services.concurrency import lock_manager
from app.services.employment import HOURLY_TYPES
from app.services.employee import (
    _get_sp_user_name_map,
    get_employee_by_id,
)
from app.services.manager_assignments import _extract_all_managers, _manager_id_to_name

logger = logging.getLogger(__name__)


class StaffWriteError(Exception):
    """A Staff Directory write to SharePoint failed.

    Raised so the route can answer 502 (an upstream dependency, not the
    caller's request, is at fault) rather than a generic 500.
    """

# snake_case wire key -> Staff Directory (SharePoint) column name. These are the
# only figures HR may change from the person panel; anything else is read-only.
EDITABLE = {
    "vacation_entitlement": "DefaultYearlyVacationDays",  # yearly vacation grant
    "sick_entitlement": "SickDayEntitlement",             # yearly sick grant
    "vacation": "CurrentVacationBalance",                 # vacation pot (days)
    "sick": "CurrentSickDayBalance",                      # sick pot (days)
    "make_up": "CurrentOvertimeBalance",                  # make-up / overtime pot
    "carry_over": "CarryOver",                            # carried-over vacation
    "payout": "Payout",                                   # banked for payout
}

# The two keys above that are yearly grants, not balances. Grants must stay
# above zero; balances may be negative or fractional (e.g. 2.125 make-up days).
ENTITLEMENTS = {"vacation_entitlement", "sick_entitlement"}

# Human-readable name per wire key, so a validation message reads for HR rather
# than echoing a raw field name.
LABELS = {
    "vacation_entitlement": "Yearly vacation entitlement",
    "sick_entitlement": "Sick day entitlement",
    "vacation": "Vacation balance",
    "sick": "Sick day balance",
    "make_up": "Make-up / overtime balance",
    "carry_over": "Carry-over balance",
    "payout": "Payout balance",
}

# The Staff Directory columns the admin person panel shows. Returned only when
# present on the record, so a blank column is simply absent rather than null.
PANEL_COLUMNS = [
    "Title", "EmailAddress", "CellNumber", "Extension", "Birthday",
    "Location", "Department", "SalaryHourly", "Supervisor", "Comments",
    "SystemCheck", "RequestAllowDate", "DefaultYearlyVacationDays",
    "SickDayEntitlement", "CurrentVacationBalance", "CurrentSickDayBalance",
    "CurrentOvertimeBalance", "CarryOver", "Payout", "Modified",
]

# ---------------------------------------------------------------------------
# Staff Directory text/choice/person fields the person panel may also change,
# saved in the same request as the numeric pots (#276).
# ---------------------------------------------------------------------------

# snake_case wire key -> Staff Directory column, for plain text/choice columns
# written as a string value. Verified column internal names and types.
TEXT_FIELDS = {
    "name": "Title",                      # display name / Staff Directory Title (text)
    "email": "EmailAddress",              # work email (text)
    "cell": "CellNumber",                 # mobile number (text)
    "location": "Location",               # office choice, required, closed list
    "department": "Department",           # department choice
    "salary_hourly": "SalaryHourly",      # choice, required: Salary / Hourly / Hourly BC
    "supervisor_text": "Supervisor",      # typed supervisor name (text, not a link)
    "system_check": "SystemCheck",        # choice: Ready / Editing
    "comments": "Comments",               # free-text notes (text)
}

# Number columns: the stored value is a number, so a digit string is coerced to
# an int and a blank clears the cell.
NUMBER_FIELDS = {
    "extension": "Extension",             # desk extension (number column)
}

# dateTime columns: compared and written on the date part only (YYYY-MM-DD), so
# a stored timestamp never reads as a change against a date the editor shows.
DATE_FIELDS = {
    "birthday": "Birthday",               # dateTime column
    "request_allow_date": "RequestAllowDate",  # can-book-vacation-until (dateTime)
}

# Fields that may never be blanked (closed/required columns the record needs).
REQUIRED_FIELDS = {"name", "location", "salary_hourly"}

# snake_case wire key -> Staff Directory person column base. A person column is
# written through its ``<base>LookupId`` companion with a SharePoint user id.
# The wire value is ``{"sp_user_id": int, "name": str}``; the link cannot be
# cleared (Graph accepting a null single-lookup is unverified), only set/changed.
PERSON_FIELDS = {
    "title_link": "TitleLink",            # the person's own Microsoft 365 link
    "supervisor_link": "SupervisorLink",  # their supervisor's Microsoft 365 link
}

# The managers wire key writes the multi-value AllManagers person column. Its
# value is ``{"ids": [int, ...], "names": [str, ...]}``; names are for history.
MANAGERS_KEY = "managers"
MANAGERS_COLUMN = "AllManagers"

# Friendly label per Staff Directory column, so the history and any validation
# message reads for HR rather than echoing a raw SharePoint column name. Covers
# both the numeric pots and the directory fields.
COLUMN_LABELS = {
    "Title": "Name",
    "EmailAddress": "Email",
    "CellNumber": "Cell",
    "Extension": "Extension",
    "Birthday": "Birthday",
    "Location": "Location",
    "Department": "Department",
    "SalaryHourly": "Salary or hourly",
    "Supervisor": "Supervisor (text)",
    "SystemCheck": "System check",
    "RequestAllowDate": "Can book vacation until",
    "Comments": "Comments",
    "TitleLink": "Person link",
    "SupervisorLink": "Supervisor link",
    "AllManagers": "Managers",
}


def _as_number(value, label: str) -> float:
    """Read one requested value as a finite number, rejecting anything else.

    Args:
        value: The submitted value for one editable field.
        label: Human name of the field, for the error message.

    Returns:
        The value as a float.

    Raises:
        ValueError: When the value is a bool, is not an int/float, or is
            NaN or infinite. The message is written for an HR reader.
    """
    if isinstance(value, bool):                      # bool is an int subclass
        raise ValueError(f"{label} must be a number.")  # reject true/false outright
    if not isinstance(value, (int, float)):          # strings, None, lists, etc.
        raise ValueError(f"{label} must be a number.")  # only real numbers allowed
    number = float(value)                            # normalise int -> float
    if not math.isfinite(number):                    # catches NaN and +/- inf
        raise ValueError(f"{label} must be a real number.")
    return number                                    # finite float, safe to store


def _current_number(value) -> float:
    """Read a stored Staff Directory value as a number, blank counting as zero.

    Args:
        value: The current column value from the record (may be missing/blank).

    Returns:
        The value as a float, or 0.0 when missing, blank, or unparseable.
    """
    if value in (None, ""):                          # missing or blank column
        return 0.0                                   # treated as a zero figure
    try:
        return float(value)                          # numbers stored as text too
    except (TypeError, ValueError):
        return 0.0                                    # never let a stray value crash


def plan_changes(current_fields: dict, requested: dict, employment_type: str | None = None) -> list[dict]:
    """Validate requested changes and return only the ones that differ. No I/O.

    Each requested key must be editable and numeric; entitlements must be above
    zero while balances may be negative or fractional. The one exception is BC
    hourly (#300): they have no vacation, so their vacation entitlement may be
    zero (never negative). A requested value equal to the current one is
    dropped, so the write touches only columns that moved.

    Args:
        current_fields: The record's current ``fields`` dict (SharePoint columns).
        requested: snake_case wire key -> new numeric value, as sent by the panel.
        employment_type: The type the record will have after this save (the
            same save may change it); defaults to the current SalaryHourly.

    Returns:
        One dict per differing field: ``{"field", "column", "before", "after"}``,
        before/after as floats.

    Raises:
        ValueError: On an unknown key, a non-number/NaN/inf/bool value, or an
            entitlement at or below zero.
    """
    changes: list[dict] = []                         # accumulates only real diffs
    if employment_type is None:                      # same save did not change the type
        employment_type = current_fields.get("SalaryHourly")
    bc = (employment_type or "").strip() in HOURLY_TYPES  # hourly: 0 vacation grant is valid
    for key, raw in requested.items():               # one pass over the request
        if key not in EDITABLE:                      # guard against stray keys
            raise ValueError(f"'{key}' is not a field that can be changed.")
        column = EDITABLE[key]                        # the SharePoint column name
        label = LABELS[key]                          # friendly name for messages
        after = _as_number(raw, label)               # validate + coerce the input
        if key in ENTITLEMENTS and after <= 0:       # grants cannot be zero/negative
            if not (key == "vacation_entitlement" and after == 0 and bc):
                raise ValueError(
                    f"{label} cannot be below zero." if bc and key == "vacation_entitlement"
                    else f"{label} must be above zero."
                )
        before = _current_number(current_fields.get(column))  # current, blank -> 0
        if before != after:                          # skip no-op values entirely
            changes.append({                         # record a genuine change
                "field": key,
                "column": column,
                "before": before,
                "after": after,
            })
    return changes                                   # possibly empty (nothing moved)


def _clean_text(value) -> str:
    """One submitted text value as a trimmed string, None counting as blank.

    Args:
        value: The submitted value for a text/choice/date field.

    Returns:
        The value trimmed to a string; ``""`` for None.
    """
    if value is None:                                # missing -> blank, never "None"
        return ""
    return str(value).strip()                        # numbers/dates coerce to text


def _lookup_id_of(value):
    """The SharePoint user id out of a person column, in either Graph shape.

    Args:
        value: A ``*LookupId`` column value, either the raw id or the complex
            ``{"LookupId": ...}`` object Graph sometimes returns.

    Returns:
        The raw id (int/str) or None when absent.
    """
    if isinstance(value, dict):                      # complex object form
        return value.get("LookupId")
    return value                                     # already the bare id (or None)


def _name_from_lookup(lookup_id, id_to_name: dict[int, str]) -> str:
    """A display name for a person-column id, using an already-read name map.

    Args:
        lookup_id: The SharePoint user id (raw int/str) or None.
        id_to_name: The ``{sp_user_id: display_name}`` map, read once per request.

    Returns:
        The display name, or ``""`` when the id is absent or unknown.
    """
    if not lookup_id:                                # nothing linked
        return ""
    try:
        return id_to_name.get(int(lookup_id), "")    # map hit, or blank when unknown
    except (TypeError, ValueError):                  # a non-numeric id is just unknown
        return ""


def _date_part(value) -> str:
    """The ``YYYY-MM-DD`` day out of a stored value, blank counting as blank.

    Args:
        value: A date/dateTime column value (ISO string) or None.

    Returns:
        The first ten characters (the calendar day), or ``""`` when blank.
    """
    if value in (None, ""):                          # missing or blank column
        return ""
    return str(value).strip()[:10]                   # drop any time part after the day


def plan_field_changes(
    current_fields: dict, before_people: dict, requested: dict
) -> list[dict]:
    """Validate requested Staff Directory field edits and return only the diffs.

    Pure (no I/O): the person/manager "before" names are resolved by the caller
    and passed in ``before_people``. Each requested key must be a known field.
    Text fields compare trimmed strings; person links compare display names;
    the managers list compares as an unordered set of names. A value equal to
    the current one is dropped so the write touches only what moved.

    Args:
        current_fields: The record's current ``fields`` dict (SharePoint columns).
        before_people: Current display values for the people fields, as
            ``{"title_link", "supervisor_link", "managers"}``. Links hold the
            resolved name; managers hold ``{"ids": [int], "names": [str]}``.
        requested: snake_case wire key -> new value, as sent by the panel. Text
            values are strings; a person link is ``{"sp_user_id", "name"}``; the
            managers key is ``{"ids": [int], "names": [str]}``.

    Returns:
        One dict per differing field: ``{"field", "column", "before", "after",
        "sp"}`` where ``field`` is the wire key, ``column`` the SharePoint
        column, ``before``/``after`` are display strings and ``sp`` is the patch
        to merge into the single update call.

    Raises:
        ValueError: On an unknown key; a blank required field; a non-numeric
            number field; a person link with no valid id or one asked to clear;
            or a managers value whose ids and names do not line up.
    """
    changes: list[dict] = []                         # only genuine diffs end up here
    for key, raw in requested.items():               # one pass over the request
        if key in TEXT_FIELDS:                        # plain text / choice column
            column = TEXT_FIELDS[key]                 # its SharePoint column name
            label = COLUMN_LABELS.get(column, column)  # friendly name for messages
            after = _clean_text(raw)                  # trim the new value
            before = _clean_text(current_fields.get(column))  # trim the stored value
            if key in REQUIRED_FIELDS and not after:   # closed/required column
                raise ValueError(f"{label} cannot be blank.")
            if before != after:                        # skip no-op values
                changes.append({
                    "field": key, "column": column,
                    "before": before, "after": after,
                    "sp": {column: after},             # write the string straight across
                })
        elif key in NUMBER_FIELDS:                     # numeric column (e.g. Extension)
            column = NUMBER_FIELDS[key]
            label = COLUMN_LABELS.get(column, column)
            after_text = _clean_text(raw)              # digits as typed, or blank to clear
            if after_text and not after_text.isdigit():  # a number column takes digits only
                raise ValueError(f"{label} must be a whole number.")
            after_val = int(after_text) if after_text else None  # None clears the cell
            before_raw = current_fields.get(column)    # stored number (may be float/text)
            before_val = None                          # normalised to an int for comparison
            if before_raw not in (None, ""):
                try:
                    before_val = int(float(before_raw))  # "123.0"/"123" -> 123
                except (TypeError, ValueError):
                    before_val = None
            if before_val != after_val:                # skip no-op values
                changes.append({
                    "field": key, "column": column,
                    "before": "" if before_val is None else str(before_val),
                    "after": after_text,
                    "sp": {column: after_val},          # int or None straight across
                })
        elif key in DATE_FIELDS:                        # dateTime column, compared by day
            column = DATE_FIELDS[key]
            after_day = _date_part(raw)                 # YYYY-MM-DD, or blank to clear
            before_day = _date_part(current_fields.get(column))  # stored day only
            if before_day != after_day:                 # skip when the day did not move
                changes.append({
                    "field": key, "column": column,
                    "before": before_day, "after": after_day,
                    "sp": {column: (after_day or None)},  # write the day, None clears it
                })
        elif key in PERSON_FIELDS:                      # single-value person link column
            column = PERSON_FIELDS[key]                 # e.g. "TitleLink"
            label = COLUMN_LABELS.get(column, column)
            if raw is None:                             # missing object -> nothing to do
                continue
            if not isinstance(raw, dict):               # must be the {sp_user_id, name} shape
                raise ValueError(f"{label} must be a selected person.")
            after_name = _clean_text(raw.get("name"))   # display name for the history
            before_name = _clean_text(before_people.get(key))  # resolved current name
            if not after_name:                          # a blank name means "clear it"
                if not before_name:                     # nothing there to begin with
                    continue                            # no change at all
                # Graph accepting a null single lookup is unverified, so clearing
                # a person link is not offered; refuse it with a readable message.
                raise ValueError(f"{label} cannot be cleared, only changed.")
            uid = _as_sp_user_id(raw.get("sp_user_id"))  # validated id, or 0 when bad
            if uid <= 0:                                # a name with no backing id
                raise ValueError(
                    f"Pick {label.lower()} from the list so it can be saved."
                )
            if before_name != after_name:               # the link actually moved
                # A person column is set through its LookupId companion; no
                # @odata.type is needed for a single lookup.
                changes.append({
                    "field": key, "column": column,
                    "before": before_name, "after": after_name,
                    "sp": {f"{column}LookupId": uid},
                })
        elif key == MANAGERS_KEY:                       # the multi-value AllManagers column
            if raw is None:                             # missing object -> nothing to do
                continue
            if not isinstance(raw, dict):               # must be the {ids, names} shape
                raise ValueError("Managers must be a list of selected people.")
            raw_ids = raw.get("ids")
            raw_names = raw.get("names")
            if not isinstance(raw_ids, list) or not isinstance(raw_names, list):
                raise ValueError("Managers must be a list of selected people.")
            if len(raw_ids) != len(raw_names):          # every name must carry its id
                raise ValueError("Each manager must be picked from the list.")
            after_ids: list[int] = []                   # validated SharePoint user ids
            for i in raw_ids:
                uid = _as_sp_user_id(i)
                if uid <= 0:                            # a manager with no backing id
                    raise ValueError("Each manager must be picked from the list.")
                after_ids.append(uid)
            after_names = [_clean_text(n) for n in raw_names]
            before = before_people.get("managers") or {}  # {"ids", "names"}
            before_ids = [int(i) for i in (before.get("ids") or [])]
            before_names = [_clean_text(n) for n in (before.get("names") or [])]
            if set(before_ids) != set(after_ids):       # order does not matter for managers
                # A multi-value lookup needs the Collection odata type plus the id list.
                changes.append({
                    "field": MANAGERS_KEY, "column": MANAGERS_COLUMN,
                    "before": ", ".join(n for n in before_names if n) or "None",
                    "after": ", ".join(n for n in after_names if n) or "None",
                    "sp": {
                        f"{MANAGERS_COLUMN}LookupId@odata.type": "Collection(Edm.Int32)",
                        f"{MANAGERS_COLUMN}LookupId": after_ids,
                    },
                })
        else:                                           # anything else is not editable
            raise ValueError(f"'{key}' is not a field that can be changed.")
    return changes                                      # possibly empty (nothing moved)


def _as_sp_user_id(value) -> int:
    """One SharePoint user id as a positive int, 0 when missing or invalid.

    Args:
        value: The submitted ``sp_user_id`` for a person pick.

    Returns:
        The id as a positive int, or 0 when it is absent, non-numeric, or <= 0.
    """
    if isinstance(value, bool):                      # bool is an int subclass; reject it
        return 0
    try:
        uid = int(value)                             # tolerate "12" and 12 alike
    except (TypeError, ValueError):                  # None, "", non-numeric text
        return 0
    return uid if uid > 0 else 0                      # a real id is always positive


def _resolve_before_people(
    current_fields: dict, requested_fields: dict, id_to_name: dict[int, str]
) -> dict:
    """Resolve the current display values for the people fields being changed.

    Pure (no I/O): the ``{sp_user_id: name}`` map is read once by the caller and
    passed in, so this adds no Graph call and stays cheap inside the lock. Only
    the people fields present in the request are resolved.

    Args:
        current_fields: The record's current ``fields`` dict.
        requested_fields: The directory-field part of the request (wire keys).
        id_to_name: The ``{sp_user_id: display_name}`` map, read once per request.

    Returns:
        ``{"title_link", "supervisor_link", "managers"}`` for keys that were
        requested; links hold the resolved name, managers hold
        ``{"ids": [int], "names": [str]}``.
    """
    before: dict = {}
    if "title_link" in requested_fields:               # resolve only when it is being edited
        before["title_link"] = _name_from_lookup(
            _lookup_id_of(current_fields.get("TitleLinkLookupId")), id_to_name
        )
    if "supervisor_link" in requested_fields:
        before["supervisor_link"] = _name_from_lookup(
            _lookup_id_of(current_fields.get("SupervisorLinkLookupId")), id_to_name
        )
    if MANAGERS_KEY in requested_fields:               # resolve the current managers
        before["managers"] = _current_managers(current_fields, id_to_name)
    return before


def _current_managers(current_fields: dict, id_to_name: dict[int, str]) -> dict:
    """The current managers from AllManagers, as aligned id and name lists.

    Each entry's name comes from its LookupValue when present, else from the
    ``{sp_user_id: name}`` map, so the history "before" reads as names even when
    Graph returned only ids. The ids and names stay index-aligned.

    Args:
        current_fields: The record's current ``fields`` dict.
        id_to_name: The ``{sp_user_id: display_name}`` map, read once per request.

    Returns:
        ``{"ids": [int], "names": [str]}``; both empty when there are none.
    """
    entries = _extract_all_managers(current_fields)    # [{sp_user_id, name}]
    ids: list[int] = []
    names: list[str] = []
    for entry in entries:
        uid = entry["sp_user_id"]
        ids.append(uid)
        # Prefer the name on the entry; fall back to the map; blank when unknown.
        names.append((entry.get("name") or "").strip() or id_to_name.get(uid, ""))
    return {"ids": ids, "names": names}


def _link_payload(field_changes: list[dict], fields_in: dict) -> dict | None:
    """The manager/supervisor links to pass the store, with names, for the ones that moved.

    Only links that actually changed are included, so a save that leaves a person
    column alone does not rewrite it. The names come from the request itself
    (the picker sends ids and names together), so the Postgres store keeps matching
    managers by name without a second lookup.

    Args:
        field_changes: The planned field diffs from ``plan_field_changes``.
        fields_in: The directory-field part of the request (wire keys).

    Returns:
        ``{"managers": [...]}`` and/or ``{"supervisor_link": {...}}`` for the links
        that changed, or None when no person column moved.
    """
    payload: dict = {}
    changed_fields = {c["field"] for c in field_changes}   # only what really moved
    if MANAGERS_KEY in changed_fields:                     # the AllManagers set changed
        managers = fields_in.get(MANAGERS_KEY) or {}
        ids = managers.get("ids") or []
        names = managers.get("names") or []
        payload["managers"] = [
            {"sp_user_id": _as_sp_user_id(uid), "name": _clean_text(name)}
            for uid, name in zip(ids, names)
        ]
    if "supervisor_link" in changed_fields:                # the single SupervisorLink changed
        link = fields_in.get("supervisor_link") or {}
        payload["supervisor_link"] = {
            "sp_user_id": _as_sp_user_id(link.get("sp_user_id")),
            "name": _clean_text(link.get("name")),
        }
    return payload or None                                 # None when no person column moved


async def apply_entitlement_changes(
    employee_id: str, requested: dict, reason: str, fields: dict | None = None
) -> dict:
    """Apply balance/entitlement and Staff Directory field changes, with a reason.

    One save covers both the numeric pots (``requested``) and the Staff Directory
    text/choice/person fields (``fields``): under the per-employee lock it
    re-reads the record, plans both diffs, writes every changed column in one
    patch call, recomputes the request-allow date when vacation or carry-over
    moved, and records one history row per field changed. If the history write
    fails the SharePoint write is kept (not undone) and the caller is told so.

    Args:
        employee_id: Staff Directory item id of the person being edited.
        requested: snake_case wire key -> new numeric value (the balance pots).
        reason: Why HR made the change; required and stored with the history.
        fields: snake_case wire key -> new value for the Staff Directory text,
            choice and person fields (see ``TEXT_FIELDS``/``PERSON_FIELDS`` and
            the managers key). Optional; omitted means no directory-field edits.

    Returns:
        ``{"employee_id", "name", "changed": [...], "history_recorded": bool}``,
        plus ``"notice"`` when the history could not be recorded. ``changed``
        merges the pot and field diffs and is empty when nothing differed (no
        write happens).

    Raises:
        ValueError: Blank reason, empty request, or an invalid requested value.
        LookupError: The employee does not exist.
        StaffWriteError: The SharePoint write failed (upstream fault).
    """
    reason = (reason or "").strip()                  # trim surrounding whitespace
    if not reason:                                   # a reason is mandatory
        raise ValueError("A reason is required.")
    requested = requested or {}                      # numeric pots (may be empty)
    fields_in = fields or {}                          # directory fields (may be empty)
    if not requested and not fields_in:              # nothing asked to change at all
        raise ValueError("No changes were provided.")

    # The people "before" names need the User Information List; read it once,
    # outside the lock, and only when a person/manager field is actually edited.
    needs_people = any(
        k in PERSON_FIELDS or k == MANAGERS_KEY for k in fields_in
    )
    id_to_name: dict[int, str] = {}
    if needs_people:
        try:
            id_to_name = await _person_names()       # one read for all people fields
        except Exception:
            logger.exception("Failed to read user name map for %s", employee_id)
            id_to_name = {}                          # degrade: before-names may be blank

    async with lock_manager.lock(employee_id):       # serialise per-employee writes
        emp = await get_employee_by_id(employee_id)  # re-read inside the lock
        if not emp:                                  # gone or never existed
            raise LookupError("Employee not found")
        current = emp.get("fields", {})              # current column values
        name = current.get("Title", "")              # person's display name

        # The type this record will have after the save decides whether a zero
        # vacation entitlement is allowed (hourly, #300).
        pot_changes = plan_changes(current, requested, fields_in.get("salary_hourly"))  # may raise
        # Resolve the "before" people from the pre-read map (no Graph call here).
        before_people = _resolve_before_people(current, fields_in, id_to_name)
        field_changes = plan_field_changes(current, before_people, fields_in)  # may raise
        changes = pot_changes + field_changes        # one merged list of real diffs
        if not changes:                              # every value already matched
            return {                                 # no write, history is trivially fine
                "employee_id": str(employee_id),
                "name": name,
                "changed": [],
                "history_recorded": True,
            }

        # One patch call carries every changed column: pots write their value
        # straight, directory fields merge their own ``sp`` patch (which may add
        # the LookupId companions and the collection odata type).
        sp_updates: dict = {c["column"]: c["after"] for c in pot_changes}
        for c in field_changes:
            sp_updates.update(c["sp"])
        # When staff live in Postgres, the manager/supervisor person columns in the
        # patch carry ids but no names, so pass the names alongside; SharePoint
        # ignores this and reads the LookupId keys. ``history_source`` stays None:
        # this service records its own history below, so the store must not log it
        # a second time.
        link_payload = _link_payload(field_changes, fields_in)
        try:
            await staff_store.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id, sp_updates,
                links=link_payload,
            )
        except Exception as exc:                      # upstream write failed
            logger.exception("Failed to write Staff Directory record for %s", employee_id)
            raise StaffWriteError("The record could not be saved.") from exc

        # Record the history right after the write succeeds, before the derived
        # recalculation (which can fail on its own and must not skip the log).
        history_recorded = True                      # assume success until it throws
        notice = None                                # extra message only on failure
        try:
            await staff_changes.record_changes(      # one row per (column, old, new)
                str(employee_id),
                name,
                [(c["column"], c["before"], c["after"]) for c in changes],
                reason,
            )
        except Exception:                            # history must not undo the save
            logger.exception(                        # keep the detail in the logs
                "Failed to record entitlement history for %s", employee_id
            )
            history_recorded = False                 # tell the caller it is missing
            notice = "The change was saved, but its history could not be recorded."

        # The request-allow date depends on vacation and carry-over, so recompute
        # it only when one of those two pots moved; but never when HR typed the
        # date itself in this save (their value wins over the derived one).
        changed_columns = {c["column"] for c in pot_changes}
        rad_moved = None                             # (before_day, after_day) when it changed
        if (("CurrentVacationBalance" in changed_columns
                or "CarryOver" in changed_columns)
                and "request_allow_date" not in fields_in):
            vacation = sp_updates.get(                # new value if changed...
                "CurrentVacationBalance",
                _current_number(current.get("CurrentVacationBalance")),  # ...else current
            )
            carry_over = sp_updates.get(
                "CarryOver",
                _current_number(current.get("CarryOver")),
            )
            before_rad = _date_part(current.get("RequestAllowDate"))  # for the history row
            try:
                new_rad = await recalculate_request_allow_date(
                    employee_id, vacation, carry_over
                )
                if new_rad and new_rad != before_rad:  # the derived date actually moved
                    rad_moved = (before_rad, new_rad)
            except Exception:                         # a stale derived date is not fatal
                logger.exception(
                    "Failed to recalculate request-allow date for %s", employee_id
                )
                extra = "The can-book-vacation date may be out of date."
                notice = f"{notice} {extra}" if notice else extra

        # When the derived date moved, record it as its own history row so the
        # log shows the automatic change next to the balance edit that caused it.
        if rad_moved and history_recorded:
            try:
                await staff_changes.record_changes(
                    str(employee_id),
                    name,
                    [("RequestAllowDate", rad_moved[0], rad_moved[1])],
                    "Recomputed automatically after a balance change.",
                )
            except Exception:                         # best-effort, like the main log
                logger.exception(
                    "Failed to record request-allow-date history for %s", employee_id
                )

        result = {                                   # the saved outcome
            "employee_id": str(employee_id),
            "name": name,
            "changed": changes,
            "history_recorded": history_recorded,
        }
        if notice:                                   # only present when something degraded
            result["notice"] = notice
        return result


async def _person_names() -> dict[int, str]:
    """Person id -> display name for the people fields, from the backend staff live in.

    After the switch a person field holds a staff id and the name comes from the
    staff table; SharePoint's user list is never read. Before it, the ids are
    SharePoint user ids, named from that list as before.

    Returns:
        ``{person id: display name}``.
    """
    if await staff_in_postgres():
        return await _manager_id_to_name()           # the staff table
    return await _get_sp_user_name_map()             # SharePoint, before the switch

async def get_staff_record(employee_id: str) -> dict | None:
    """Read one person's Staff Directory record for the admin person panel.

    Returns the panel columns, the resolved people fields, and the change
    history. Each person/history lookup degrades on its own: a failure there is
    logged and yields an empty value for that part rather than failing the whole
    read, since the panel is still useful without it.

    Args:
        employee_id: Staff Directory item id of the person to show.

    Returns:
        ``{"employee_id", "fields", "people", "history"}`` where ``fields`` holds
        only the panel columns present on the record, ``people`` holds the
        resolved ``title_link`` / ``supervisor_link`` names and a ``managers``
        list of ``{"sp_user_id", "name"}``, and ``history`` is the change log.
        ``None`` when the record does not exist.
    """
    emp = await get_employee_by_id(employee_id)      # the raw record (or None)
    if not emp:                                      # unknown id
        return None
    fields = emp.get("fields", {})                   # SharePoint column values

    # Copy across only the panel columns that are actually present.
    panel = {col: fields[col] for col in PANEL_COLUMNS if col in fields}

    # People fields start empty and are filled in best-effort below.
    people = {"title_link": "", "supervisor_link": "", "managers": []}

    # One User Information List read covers both person links and the managers;
    # a failure degrades to blank names rather than failing the whole panel.
    try:
        id_to_name = await _person_names()
    except Exception:
        logger.exception("Failed to read user name map for %s", employee_id)
        id_to_name = {}

    # Resolve the person's own link and their supervisor link from the map.
    people["title_link"] = _name_from_lookup(
        _lookup_id_of(fields.get("TitleLinkLookupId")), id_to_name
    )
    people["supervisor_link"] = _name_from_lookup(
        _lookup_id_of(fields.get("SupervisorLinkLookupId")), id_to_name
    )

    # Managers as {sp_user_id, name} so the editor can show the picks and send
    # their ids straight back; a blank entry name is filled from the map.
    managers = _extract_all_managers(fields)         # [{sp_user_id, name}]
    people["managers"] = [
        {
            "sp_user_id": m["sp_user_id"],
            "name": (m.get("name") or "").strip() or id_to_name.get(m["sp_user_id"], ""),
        }
        for m in managers
    ]

    try:                                             # the recorded change history
        history = await staff_changes.list_changes(str(employee_id))
    except Exception:
        logger.exception("Failed to read change history for %s", employee_id)
        history = []                                 # panel still renders without it

    return {                                         # the assembled panel payload
        "employee_id": str(employee_id),
        "fields": panel,
        "people": people,
        "history": history,
    }
