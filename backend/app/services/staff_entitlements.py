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
from app.graph.sharepoint import sp_client
from app.services import staff_changes
from app.services.balance import recalculate_request_allow_date
from app.services.concurrency import lock_manager
from app.services.employee import (
    _get_sp_user_name_map,
    get_all_managers_for_employee,
    get_employee_by_id,
    resolve_person_field_name,
)

logger = logging.getLogger(__name__)

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

# snake_case wire key -> Staff Directory column, for plain text/choice/date
# columns written as a string value. Name must stay non-blank; the rest may be
# emptied. "name" is the person's display name (the Title column).
TEXT_FIELDS = {
    "name": "Title",                      # display name / Staff Directory Title
    "email": "EmailAddress",              # work email
    "cell": "CellNumber",                 # mobile number
    "extension": "Extension",             # desk extension
    "birthday": "Birthday",               # free-text birthday (e.g. "Mar 3")
    "location": "Location",               # office / province choice
    "department": "Department",           # department choice
    "salary_hourly": "SalaryHourly",      # Salary / Hourly / Hourly BC
    "supervisor_text": "Supervisor",      # typed supervisor name (not a link)
    "system_check": "SystemCheck",        # Ready / Editing gate
    "request_allow_date": "RequestAllowDate",  # can-book-vacation-until date
    "comments": "Comments",               # free-text notes
}

# snake_case wire key -> Staff Directory person column base. A person column is
# written through its ``<base>LookupId`` companion with a SharePoint user id.
# The wire value is ``{"sp_user_id": int|None, "name": str}``; a blank id clears
# the link. The display name is used for the change history only.
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


def plan_changes(current_fields: dict, requested: dict) -> list[dict]:
    """Validate requested changes and return only the ones that differ. No I/O.

    Each requested key must be editable and numeric; entitlements must be above
    zero while balances may be negative or fractional. A requested value equal to
    the current one is dropped, so the write touches only columns that moved.

    Args:
        current_fields: The record's current ``fields`` dict (SharePoint columns).
        requested: snake_case wire key -> new numeric value, as sent by the panel.

    Returns:
        One dict per differing field: ``{"field", "column", "before", "after"}``,
        before/after as floats.

    Raises:
        ValueError: On an unknown key, a non-number/NaN/inf/bool value, or an
            entitlement at or below zero.
    """
    changes: list[dict] = []                         # accumulates only real diffs
    for key, raw in requested.items():               # one pass over the request
        if key not in EDITABLE:                      # guard against stray keys
            raise ValueError(f"'{key}' is not a field that can be changed.")
        column = EDITABLE[key]                        # the SharePoint column name
        label = LABELS[key]                          # friendly name for messages
        after = _as_number(raw, label)               # validate + coerce the input
        if key in ENTITLEMENTS and after <= 0:       # grants cannot be zero/negative
            raise ValueError(f"{label} must be above zero.")
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
            ``{"title_link", "supervisor_link", "managers"}`` (names / name list).
        requested: snake_case wire key -> new value, as sent by the panel. Text
            values are strings; a person link is ``{"sp_user_id", "name"}``; the
            managers key is ``{"ids": [int], "names": [str]}``.

    Returns:
        One dict per differing field: ``{"field", "column", "before", "after",
        "sp"}`` where ``field`` is the wire key, ``column`` the SharePoint
        column, ``before``/``after`` are display strings and ``sp`` is the patch
        to merge into the single update call.

    Raises:
        ValueError: On an unknown key, or a blank name (the Title is required).
    """
    changes: list[dict] = []                         # only genuine diffs end up here
    for key, raw in requested.items():               # one pass over the request
        if key in TEXT_FIELDS:                        # plain text / choice / date column
            column = TEXT_FIELDS[key]                 # its SharePoint column name
            after = _clean_text(raw)                  # trim the new value
            before = _clean_text(current_fields.get(column))  # trim the stored value
            if key == "name" and not after:           # the display name is mandatory
                raise ValueError("Name cannot be blank.")
            if before != after:                        # skip no-op values
                changes.append({
                    "field": key, "column": column,
                    "before": before, "after": after,
                    "sp": {column: after},             # write the string straight across
                })
        elif key in PERSON_FIELDS:                     # single-value person link column
            column = PERSON_FIELDS[key]                # e.g. "TitleLink"
            raw = raw or {}                            # tolerate a bare null
            sp_user_id = raw.get("sp_user_id")         # SharePoint user id, or None to clear
            after_name = _clean_text(raw.get("name"))  # display name for the history
            before_name = _clean_text(before_people.get(key))  # resolved current name
            if before_name != after_name:              # the link actually moved
                # A person column is set through its LookupId companion; None
                # clears it. @odata.type is not needed for a single lookup.
                patch = {f"{column}LookupId": int(sp_user_id) if sp_user_id else None}
                changes.append({
                    "field": key, "column": column,
                    "before": before_name, "after": after_name, "sp": patch,
                })
        elif key == MANAGERS_KEY:                      # the multi-value AllManagers column
            raw = raw or {}
            ids = [int(i) for i in (raw.get("ids") or [])]     # SharePoint user ids to set
            after_names = [_clean_text(n) for n in (raw.get("names") or []) if _clean_text(n)]
            before_names = [_clean_text(n) for n in (before_people.get("managers") or []) if _clean_text(n)]
            if set(before_names) != set(after_names):  # order does not matter for managers
                # A multi-value lookup needs the Collection odata type plus the id list.
                patch = {
                    f"{MANAGERS_COLUMN}LookupId@odata.type": "Collection(Edm.Int32)",
                    f"{MANAGERS_COLUMN}LookupId": ids,
                }
                changes.append({
                    "field": MANAGERS_KEY, "column": MANAGERS_COLUMN,
                    "before": ", ".join(before_names) or "None",
                    "after": ", ".join(after_names) or "None",
                    "sp": patch,
                })
        else:                                          # anything else is not editable
            raise ValueError(f"'{key}' is not a field that can be changed.")
    return changes                                     # possibly empty (nothing moved)


async def _resolve_before_people(current_fields: dict, requested_fields: dict) -> dict:
    """Resolve the current display values for the people fields being changed.

    Only the people fields present in the request are resolved, so an edit that
    touches no person column makes no extra Graph calls. Each lookup is
    best-effort: a failure yields an empty value for that field rather than
    failing the whole save.

    Args:
        current_fields: The record's current ``fields`` dict.
        requested_fields: The directory-field part of the request (wire keys).

    Returns:
        ``{"title_link", "supervisor_link", "managers"}`` with the resolved
        current name / name list, for keys that were requested.
    """
    before: dict = {}
    if "title_link" in requested_fields:               # resolve only when it is being edited
        try:
            before["title_link"] = await resolve_person_field_name(
                current_fields.get("TitleLinkLookupId")
            )
        except Exception:
            logger.exception("Failed to resolve current TitleLink name")
            before["title_link"] = ""                  # unknown -> treat as blank
    if "supervisor_link" in requested_fields:
        try:
            before["supervisor_link"] = await resolve_person_field_name(
                current_fields.get("SupervisorLinkLookupId")
            )
        except Exception:
            logger.exception("Failed to resolve current SupervisorLink name")
            before["supervisor_link"] = ""
    if MANAGERS_KEY in requested_fields:               # resolve the current manager names
        before["managers"] = await _current_manager_names(current_fields)
    return before


async def _current_manager_names(current_fields: dict) -> list[str]:
    """The current manager display names from the AllManagers column.

    Uses each entry's LookupValue when present and resolves the rest through the
    user-id -> name map, so the history "before" reads as names even when Graph
    returned only ids.

    Args:
        current_fields: The record's current ``fields`` dict.

    Returns:
        The manager names (possibly empty); never raises.
    """
    entries = current_fields.get(MANAGERS_COLUMN)      # the raw AllManagers value
    if not entries or not isinstance(entries, list):   # no managers / unexpected shape
        return []
    names: list[str] = []
    unresolved: list[int] = []                         # ids whose LookupValue was blank
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("LookupValue") or "").strip()
        if name:
            names.append(name)                         # name already present on the entry
        elif entry.get("LookupId"):
            unresolved.append(int(entry["LookupId"]))  # fill it in from the map below
    if unresolved:
        try:
            id_to_name = await _get_sp_user_name_map()  # one User Information List read
            names.extend(id_to_name.get(uid, "") for uid in unresolved)
        except Exception:
            logger.exception("Failed to resolve current manager names")
    return [n for n in names if n]                     # drop any that stayed blank


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
    """
    reason = (reason or "").strip()                  # trim surrounding whitespace
    if not reason:                                   # a reason is mandatory
        raise ValueError("A reason is required.")
    requested = requested or {}                      # numeric pots (may be empty)
    fields_in = fields or {}                          # directory fields (may be empty)
    if not requested and not fields_in:              # nothing asked to change at all
        raise ValueError("No changes were provided.")

    async with lock_manager.lock(employee_id):       # serialise per-employee writes
        emp = await get_employee_by_id(employee_id)  # re-read inside the lock
        if not emp:                                  # gone or never existed
            raise LookupError("Employee not found")
        current = emp.get("fields", {})              # current column values
        name = current.get("Title", "")              # person's display name

        pot_changes = plan_changes(current, requested)  # numeric diff (may raise)
        # The people "before" names need Graph; resolve only the ones being edited.
        before_people = await _resolve_before_people(current, fields_in)
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
        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id, sp_updates,
        )

        # The request-allow date depends on vacation and carry-over, so recompute
        # it only when one of those two pots moved; the unchanged figure comes
        # from the record so the recalculation sees the full, current pair.
        changed_columns = {c["column"] for c in pot_changes}
        if "CurrentVacationBalance" in changed_columns or "CarryOver" in changed_columns:
            vacation = sp_updates.get(                # new value if changed...
                "CurrentVacationBalance",
                _current_number(current.get("CurrentVacationBalance")),  # ...else current
            )
            carry_over = sp_updates.get(
                "CarryOver",
                _current_number(current.get("CarryOver")),
            )
            await recalculate_request_allow_date(employee_id, vacation, carry_over)

        # Record the history last; the SharePoint write already succeeded. Pots
        # record numeric before/after, directory fields record display strings.
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

        result = {                                   # the saved outcome
            "employee_id": str(employee_id),
            "name": name,
            "changed": changes,
            "history_recorded": history_recorded,
        }
        if notice:                                   # only present when history failed
            result["notice"] = notice
        return result


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
        resolved ``title_link`` / ``supervisor_link`` names and ``managers`` list,
        and ``history`` is the change log. ``None`` when the record does not exist.
    """
    emp = await get_employee_by_id(employee_id)      # the raw record (or None)
    if not emp:                                      # unknown id
        return None
    fields = emp.get("fields", {})                   # SharePoint column values

    # Copy across only the panel columns that are actually present.
    panel = {col: fields[col] for col in PANEL_COLUMNS if col in fields}

    # People fields start empty and are filled in best-effort below.
    people = {"title_link": "", "supervisor_link": "", "managers": []}

    try:                                             # resolve the person's own link
        people["title_link"] = await resolve_person_field_name(
            fields.get("TitleLinkLookupId")          # Graph returns the *LookupId
        )
    except Exception:
        logger.exception("Failed to resolve TitleLink for %s", employee_id)

    try:                                             # resolve the supervisor link
        people["supervisor_link"] = await resolve_person_field_name(
            fields.get("SupervisorLinkLookupId")
        )
    except Exception:
        logger.exception("Failed to resolve SupervisorLink for %s", employee_id)

    try:                                             # all managers, by display name
        managers = await get_all_managers_for_employee(emp)
        people["managers"] = [
            m.get("fields", {}).get("Title", "") for m in managers
        ]
    except Exception:
        logger.exception("Failed to resolve managers for %s", employee_id)

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
