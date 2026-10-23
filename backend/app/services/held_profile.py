"""Add employee on a name already on staff: edit that record instead (#444).

A held request carries the name a new hire typed. When the supervisor (or an
admin, from the Held Requests list) presses Add and that name is already on
staff, the person usually is not new: they moved, or their record lacks the
address they signed in with. Creating a second record would split their
balances and history, so Add employee opens the existing record as a profile
edit instead, with what is on record now next to each field.

The supervisor decides every change (moves are the manager's job, never the
employee's). Because anyone can type a staff member's name with their own email,
three guards apply: the record is shown before anything changes, a reason is
required, and the old email address is told at once when it is replaced.
Managers taken off the person are told too.

When the record ends up carrying the address the request came from, the held
request is released as a normal request. Otherwise it stays held, with a note
saying why, and nothing is guessed.

Every write goes through ``staff_entitlements.apply_entitlement_changes``, the
same service the admin and manager profile edits use, so validation, the
per-employee lock, the staff store write and the change history are identical.
"""
import html
import logging
from datetime import datetime

from app.config import settings
from app.database import async_session
from app.graph.email import send_email
from app.models import HeldRequest
from app.repositories.postgres.staff import is_archived
from app.repositories.staff_store import staff_in_postgres, staff_store
from app.services import held_requests
from app.services.employee import get_employee_by_email, get_employee_by_id, get_employee_by_name
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.manager_assignments import _extract_all_managers
from app.services.staff_cleanup import clean_email, collapse_whitespace
from app.services.staff_entitlements import apply_entitlement_changes

logger = logging.getLogger(__name__)

# What the profile edit may change: the fields the form shows, nothing more.
# Person links and the manager list are set here, never taken from the client.
EDIT_FIELDS = ("email", "department", "salary_hourly", "location")
EDIT_POTS = ("vacation_entitlement", "sick_entitlement", "vacation", "sick")
MANAGER_MODES = ("replace", "alongside")       # take over the person, or join their managers


class HeldProfileError(ValueError):
    """A profile edit that cannot go ahead; the message is for the person editing.

    Attributes:
        status_code: The HTTP status the route should answer with.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _name_key(name: str | None) -> str:
    """A name as compared for "already on staff": spacing and case ignored."""
    return collapse_whitespace(name or "").lower()


async def same_name_records(name: str) -> list[dict]:
    """Active staff records whose name matches the held request's name.

    Args:
        name: The name the new hire typed.

    Returns:
        Every non-archived record with the same name (spacing and case
        ignored), in directory order. Usually none or one.
    """
    key = _name_key(name)
    if not key:
        return []
    items = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)   # one directory read
    return [
        item for item in items
        if _name_key(item.get("fields", {}).get("Title")) == key and not is_archived(item.get("fields", {}))
    ]


def _number(value) -> float:
    """A stored pot value as a number; blank reads as 0."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def record_summary(record: dict) -> dict:
    """What the profile edit shows as "on record now" for one staff record.

    Args:
        record: The ``{"id", "fields"}`` staff item.

    Returns:
        snake_case values for each editable field, plus the managers by name.
    """
    f = record.get("fields", {})
    return {
        "employee_id": str(record["id"]),
        "name": collapse_whitespace(f.get("Title")),
        "email": (f.get("EmailAddress") or "").strip(),
        "department": f.get("Department") or "",
        "salary_hourly": f.get("SalaryHourly") or "",
        "location": f.get("Location") or "",
        "managers": [m["name"] for m in _extract_all_managers(f)],
        "vacation_entitlement": _number(f.get("DefaultYearlyVacationDays")),
        "sick_entitlement": _number(f.get("SickDayEntitlement")),
        "vacation": _number(f.get("CurrentVacationBalance")),
        "sick": _number(f.get("CurrentSickDayBalance")),
    }


async def _load(held_id: int) -> HeldRequest | None:
    """One held request row, or None."""
    async with async_session() as session:
        return await session.get(HeldRequest, held_id)


async def held_for_add(held_id: int, supervisor_id: str | None) -> dict | None:
    """What Add employee needs to know about a held request before showing a form.

    Args:
        held_id: The held request the Add link names.
        supervisor_id: The staff id of the manager opening it, or None for an
            admin (who acts for whoever holds it now).

    Returns:
        None when there is no such request, or a manager it was never sent to
        asks. Otherwise ``{"held_id", "name",
        "email", "department", "summary", "supervisor_name", "state",
        "request_number", "set_up_on", "existing", "same_name_count"}``. State
        is "open" only while this manager (or, for an admin, anyone) still holds
        it; then the details are filled and ``existing`` is the record with the
        same name, when exactly one exists. In any other state only the name and
        what happened are returned.
    """
    row = await _load(held_id)
    if row is None:
        return None
    if supervisor_id is not None:                             # a manager: only one it was ever sent to
        involved = {str(e.get("id")) for e in (row.supervisors_involved or [])} | {str(row.supervisor_id)}
        if str(supervisor_id) not in involved:
            return None                                       # reads as "not found", like a missing id
    holder = supervisor_id if supervisor_id is not None else row.supervisor_id   # an admin acts for the holder
    state = await held_requests.not_on_team_link_state(held_id, str(holder)) or {"state": "declined"}
    # Once it no longer waits on them, they learn what happened and nothing else:
    # not who holds it now, not the person's email, not the request (#462).
    result = {
        "held_id": row.id, "name": row.name, "email": "", "department": "", "summary": "",
        "supervisor_name": "", "state": state["state"],
        "request_number": state.get("request_number", ""), "set_up_on": state.get("set_up_on", ""),
        "note": "", "existing": None, "same_name_count": 0,
    }
    if state["state"] == "open":                              # only the one who holds it sees the details
        result.update({
            "email": row.email, "department": row.department or "",
            "summary": held_requests.describe_form(row.request_type, row.form_data),
            "supervisor_name": row.supervisor_name, "note": row.last_error or "",
        })
        matches = await same_name_records(row.name)
        result["same_name_count"] = len(matches)
        if len(matches) == 1:
            result["existing"] = record_summary(matches[0])
    return result


async def _link_id(staff_id: str, email: str) -> int:
    """The id a supervisor takes in a person's ``AllManagers`` list.

    Args:
        staff_id: The supervisor's staff id.
        email: Their email (resolved to a Microsoft 365 id before the switch).

    Returns:
        The lookup id.

    Raises:
        HeldProfileError: (SharePoint only) when the email has no account.
    """
    if await staff_in_postgres():                             # after the switch the staff id is the identifier
        return int(staff_id)
    sp_user_id = await _resolve_user_lookup_id(email)          # before it, the Microsoft 365 user id
    if not sp_user_id:
        raise HeldProfileError("The supervisor's record does not resolve to a Microsoft 365 account.")
    return int(sp_user_id)


def _readable_today() -> str:
    """Today in Toronto, e.g. "Nov 2, 2026"."""
    day = datetime.now(held_requests.TORONTO).date()
    return f"{day:%b} {day.day}, {day.year}"


async def _tell_old_email(old: str, new: str, person: str, actor: str) -> None:
    """Tell the address that was replaced, so a wrong change is caught at once."""
    await send_email(
        to=[old], subject="Your staff email was changed",
        html_body=(f"<p>{html.escape(actor)} changed the email on {html.escape(person)}'s staff record "
                   f"to {html.escape(new)} on {_readable_today()}.</p>"
                   "<p>If this was not you, tell HR.</p>"),
    )


async def _tell_removed_manager(manager_name: str, person: str, actor: str) -> None:
    """Tell a manager taken off the person that they moved off the team."""
    record = await get_employee_by_name(manager_name)
    address = (record or {}).get("fields", {}).get("EmailAddress", "")
    if not address:                                           # no address on file: nothing to send
        logger.warning("No email for removed manager %s", manager_name)
        return
    await send_email(
        to=[address], subject=f"{person} moved off your team",
        html_body=(f"<p>{html.escape(actor)} updated {html.escape(person)}'s record on {_readable_today()}, "
                   "and they are no longer on your team. Nothing is needed from you.</p>"),
    )


async def update_existing_from_held(
    held_id: int, *, actor_name: str, supervisor_id: str | None, changes: dict, fields: dict,
    manager_mode: str, reason: str, source: str,
) -> dict:
    """Save the profile edit of the record that already has the held request's name.

    Args:
        held_id: The held request the edit comes from.
        actor_name: Who is saving (a manager, or an admin), for the history and notices.
        supervisor_id: The manager's staff id, who must be the one holding the
            request; None for an admin, who acts for whoever holds it.
        changes: Balance and entitlement wire keys -> new values (EDIT_POTS only).
        fields: Directory wire keys -> new values (EDIT_FIELDS only).
        manager_mode: "replace" (the supervisor becomes the only manager) or
            "alongside" (the supervisor joins the managers on record).
        reason: Why; required.
        source: The history channel ("manager_dashboard" or "admin_dashboard").

    Returns:
        ``{"employee_id", "name", "changed", "released", "held_status", "note"}``.
        ``released`` lists the release outcomes; ``held_status`` is "released"
        when the request went through and "held" when the record still lacks
        the request's address (then ``note`` says why).

    Raises:
        HeldProfileError: 404 no such request; 409 it is no longer this
            manager's to act on, two records share the name, or none does; 400
            a blank reason, a bad mode, a field that cannot be edited, an email
            already on someone else's record, or the person's own record.
    """
    reason = (reason or "").strip()
    if not reason:
        raise HeldProfileError("A reason is required.")
    if manager_mode not in MANAGER_MODES:
        raise HeldProfileError("Choose whether to replace their managers or join them.")
    extra = (set(fields) - set(EDIT_FIELDS)) | (set(changes) - set(EDIT_POTS))
    if extra:                                                 # only what the form shows
        raise HeldProfileError(f"'{sorted(extra)[0]}' cannot be changed here.")

    row = await _load(held_id)
    if row is None:
        raise HeldProfileError("Held request not found.", 404)
    if row.status != "held" or (supervisor_id is not None and str(row.supervisor_id) != str(supervisor_id)):
        raise HeldProfileError("This request is no longer waiting for you.", 409)
    matches = await same_name_records(row.name)
    if len(matches) != 1:                                     # none (use Add) or ambiguous (ask HR)
        raise HeldProfileError(
            "Two people on staff have this name. Ask HR which record is theirs." if matches
            else "Nobody on staff has this name any more. Use Add employee.", 409,
        )
    record = matches[0]
    employee_id = str(record["id"])
    if employee_id == str(row.supervisor_id):                 # never the holder's own record, admin or not
        raise HeldProfileError("That is the supervisor's own record; it cannot be changed from here.")
    before = record_summary(record)

    # A new email must not already belong to someone else: that would give two
    # records one sign-in.
    new_email = clean_email(fields.get("email")) if "email" in fields else clean_email(before["email"])
    if "email" in fields:
        fields = {**fields, "email": new_email}
        owner = await get_employee_by_email(new_email) if new_email else None
        if owner and str(owner["id"]) != employee_id:
            raise HeldProfileError(f"{new_email} is already on {owner['fields'].get('Title') or 'another person'}'s record.")

    # The supervisor who holds the request, as a manager entry.
    supervisor = await get_employee_by_id(row.supervisor_id)
    sup_fields = (supervisor or {}).get("fields", {})
    sup_name = collapse_whitespace(sup_fields.get("Title")) or row.supervisor_name
    sup_link = await _link_id(row.supervisor_id, sup_fields.get("EmailAddress") or row.supervisor_email)
    supervisor_entry = {"sp_user_id": sup_link, "name": sup_name}
    # The managers on record as the save reads them under its lock, kept for the
    # "moved off your team" notices.
    seen: dict = {"current": []}

    def managers_from(current_fields: dict) -> dict:
        """The new managers, worked out from the record as read under the lock.

        Args:
            current_fields: The record's fields, freshly re-read by the save.

        Returns:
            ``{"ids", "names"}`` for the managers key.
        """
        current = _extract_all_managers(current_fields)      # nobody added meanwhile is lost
        seen["current"] = current
        if manager_mode == "replace" or not current:
            managers = [supervisor_entry]
        else:                                                 # alongside: keep everyone, add the supervisor once
            managers = current + ([] if any(m["sp_user_id"] == sup_link for m in current) else [supervisor_entry])
        return {"ids": [m["sp_user_id"] for m in managers], "names": [m["name"] for m in managers]}

    saved = await apply_entitlement_changes(
        employee_id, changes, f"Changed by {actor_name} from {row.name}'s held request: {reason}",
        fields, source=source, managers_from=managers_from,
    )
    # Replace takes off everyone but the supervisor; alongside takes off nobody.
    removed = [m for m in seen["current"] if m["sp_user_id"] != sup_link] if manager_mode == "replace" else []

    # The notices: the replaced address, and every manager taken off.
    person = before["name"]
    if "email" in fields and before["email"] and clean_email(before["email"]) != new_email:
        try:
            await _tell_old_email(before["email"], new_email, person, actor_name)
        except Exception:  # noqa: BLE001 - the save stands; logged for a follow-up
            logger.exception("Could not tell the old address about the email change on %s", employee_id)
    for gone in removed:
        try:
            await _tell_removed_manager(gone["name"], person, actor_name)
        except Exception:  # noqa: BLE001 - informational only
            logger.exception("Could not tell a removed manager about %s", employee_id)

    # Release only when the record now carries the address the request came from.
    if new_email == clean_email(row.email):
        results = await held_requests.release_held_requests(row.email, only_id=row.id)
        released = [{"held_id": r.held_id, "status": r.status, "detail": r.detail} for r in results]
        status = results[0].status if results else "held"
        return {**saved, "released": released, "held_status": status, "note": ""}
    note = f"Record #{employee_id} was updated, but its email is not {row.email}, so the request is still held."
    async with async_session() as session:                    # keep the reason on the row for the admin list
        stored = await session.get(HeldRequest, row.id)
        if stored is not None:
            stored.last_error = note
            await session.commit()
    return {**saved, "released": [], "held_status": "held", "note": note}
