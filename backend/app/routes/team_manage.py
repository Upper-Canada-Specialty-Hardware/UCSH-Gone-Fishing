"""Manager control over their own team (#362, #363).

A manager gets admin-level control, but only over the people who list them in
their ``AllManagers`` field. Three things live here, apart from the read-only
``/team/*`` endpoints in ``routes/dashboard.py``:

- change a teammate's balances and entitlements, with a required reason, through
  the same service the admin person panel uses (``staff_entitlements``), so the
  validation and the live-update notify are identical (#362);
- add a person to the manager's own team and remove the manager from a person's
  team, through ``manager_assignments.update_employee_managers`` so the write
  goes through ``staff_store`` and open dashboards re-read (#363);
- tell the UI which staff a manager may still add, and whether removing
  themselves would leave a person with no manager at all.

Every change is recorded under the manager's name, channel ``manager_dashboard``,
exactly as an admin change is recorded under ``admin_dashboard``. All of it is
token-authenticated through the shared ``AuthUser`` and refused (403) for anyone
not on the calling manager's team.
"""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
# The read-side helpers live in the dashboard router; reuse them so team
# membership is decided one way everywhere (the brief says to use these).
from app.routes.dashboard import (
    AuthUser,
    DashboardUser,
    _format_employee,
    _is_in_all_managers,
    _refuse_if_paused,
    _require_role,
)
from app.repositories.postgres.staff import is_archived  # tells an archived record apart (#119)
from app.repositories.staff_store import staff_in_postgres, staff_store
from app.services.concurrency import lock_manager  # serialise the AllManagers read-modify-write
from app.services.employee import get_employee_by_id
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.manager_assignments import _extract_all_managers, update_employee_managers
from app.services.staff_entitlements import StaffWriteError, apply_entitlement_changes, get_staff_record
from app.services.switch_pause import SystemPaused  # the mid-switch write refusal -> 503

router = APIRouter()

# The channel every manager-dashboard change is recorded under, mirroring the
# admin dashboard's "admin_dashboard". Inside StaffChange.source's 24-char limit.
MANAGER_CHANNEL = "manager_dashboard"


async def _signed_in_manager(user: DashboardUser) -> tuple[dict, str]:
    """The calling manager's record and display name, or an error.

    Args:
        user: The signed-in dashboard user (from the signed link).

    Returns:
        ``(record, name)`` where record is the ``{"id", "fields"}`` staff item.

    Raises:
        HTTPException: 403 when the user is neither manager nor admin, 404 when
            their own staff record cannot be found.
    """
    _require_role(user, "manager", "admin")              # employees cannot reach team control
    manager = await get_employee_by_id(user.user_id)     # the caller's own staff record
    if not manager:                                      # signed in but no record: cannot act
        raise HTTPException(status_code=404, detail="Your own staff record could not be found")
    return manager, manager["fields"].get("Title", "")   # name drives the team membership check


def _may_add(manager_fields: dict, person_fields: dict) -> str | None:
    """Why this manager may not add this person to their team, or None when they may.

    The single place the "who may a manager add" rule lives, so it can be
    tightened later (#362). The owner's ruling (2026-10-19): a manager may add any
    active staff member except themselves; other managers and people who already
    have managers are allowed, and their existing managers stay. So the only
    refusals are the manager themselves, an archived person, and someone already
    on this manager's team (adding them again would do nothing). The picker uses
    this to decide who to offer; the add endpoint treats "already on the team" as
    a harmless no-op and uses this only for the hard refusals.

    Args:
        manager_fields: The calling manager's Staff Directory fields.
        person_fields: The candidate person's Staff Directory fields.

    Returns:
        A plain refusal message, or None when the add is allowed.
    """
    manager_name = (manager_fields.get("Title") or "").strip()
    person_name = (person_fields.get("Title") or "").strip()
    if not person_name or person_name == manager_name:   # no blank rows, and not yourself
        return "You cannot add yourself to your own team."
    if is_archived(person_fields):                       # archived people are out of every team (#119)
        return "That person has left and cannot be added to a team."
    if _is_in_all_managers(person_fields, manager_name):  # already lists me: nothing to add
        return "That person is already on your team."
    return None


async def _require_teammate(employee_id: str, manager_name: str) -> dict:
    """Load a person and confirm they are on the calling manager's team.

    Args:
        employee_id: Staff Directory item id of the person being acted on.
        manager_name: The calling manager's display name.

    Returns:
        The person's ``{"id", "fields"}`` staff item.

    Raises:
        HTTPException: 404 when no such person exists, 403 when they are not on
            the manager's team or have been archived (an archived person is
            changed from the admin staff screen, not a team route) (#119).
    """
    target = await get_employee_by_id(employee_id)       # the person being changed
    if not target:                                       # unknown id
        raise HTTPException(status_code=404, detail="Employee not found")
    fields = target.get("fields", {})
    if not _is_in_all_managers(fields, manager_name):    # name-based membership
        raise HTTPException(status_code=403, detail="That person is not on your team.")
    if is_archived(fields):                              # archived: not editable from a team route (#119)
        raise HTTPException(status_code=403, detail="That person has been archived.")
    return target


async def _manager_link_id(user: DashboardUser, manager_fields: dict) -> int:
    """The id the manager occupies in a person's ``AllManagers`` list.

    After the switch a manager is identified by their own staff id (there is no
    SharePoint user list to resolve against), exactly as ``POST /team/employees``
    records a new hire's supervisor. Before the switch it is the Microsoft 365
    user id resolved from the manager's email (#265).

    Args:
        user: The signed-in manager.
        manager_fields: The manager's Staff Directory fields (for their email).

    Returns:
        The manager's ``AllManagers`` lookup id.

    Raises:
        HTTPException: 400 when (SharePoint only) the manager's record does not
            resolve to a Microsoft 365 account, so they cannot be linked.
    """
    if await staff_in_postgres():                        # Postgres: the staff id is the identifier
        return int(user.user_id)
    email = (manager_fields.get("EmailAddress") or "").strip()  # SharePoint: resolve the user id
    sp_user_id = await _resolve_user_lookup_id(email)
    if not sp_user_id:                                   # no Microsoft 365 account behind the record
        raise HTTPException(
            status_code=400,
            detail="Your own record does not resolve to a Microsoft 365 account, "
                   "so you cannot be added as a manager.",
        )
    return int(sp_user_id)


# ============================
# #362 - change a teammate's balances and entitlements
# ============================

class TeamEntitlementChanges(BaseModel):
    """Body for the team entitlements PATCH: the new balance values and a reason.

    Managers may change only the numeric pots (the two entitlements and the five
    balances); the directory fields (name, managers, system check, ...) stay
    admin-only, so there is no ``fields`` key here.

    Attributes:
        changes: snake_case wire key -> new numeric value (see
            ``staff_entitlements.EDITABLE``).
        reason: Why the manager made the change; required and recorded.
    """
    # ``Any`` not ``float``: the service's ``_as_number`` does the real
    # type-checking and rejects non-numbers with a message written for the UI.
    changes: dict[str, Any] = {}
    reason: str


@router.patch("/team/members/{employee_id}/entitlements")
async def team_update_entitlements(user: AuthUser, employee_id: str, body: TeamEntitlementChanges):
    """Change one teammate's balances and entitlements, with a recorded reason (#362).

    The manager-scoped twin of the admin person panel's save: it reuses the admin
    service (``apply_entitlement_changes``), so the same validation, the same
    per-employee lock and the same ``staff_store`` write (and its live-update
    notify) all apply. The change is recorded under the manager's name, channel
    ``manager_dashboard``. Only the manager's own team may be edited.

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate being edited.
        body: The new balance values and the reason.

    Returns:
        The service result: what changed and whether the history was recorded.

    Raises:
        HTTPException: 403 for a non-teammate, 503 when processing is off, 400 on
            a blank reason or bad value, 404 when the person is gone, 502 on a
            write failure.
    """
    manager, manager_name = await _signed_in_manager(user)   # role + the caller's name
    if str(employee_id) == str(user.user_id):                 # a manager cannot change their own figures
        raise HTTPException(status_code=403, detail="You cannot change your own balances or entitlements.")
    await _require_teammate(employee_id, manager_name)        # 403 unless they are on my team (and not archived)
    if not settings.PROCESSING_ENABLED:                       # same gate as the admin save
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _refuse_if_paused()                                 # same 503 as the other write routes mid-switch
    reason = (body.reason or "").strip()                      # the manager's own words
    if not reason:                                            # a reason is mandatory
        raise HTTPException(status_code=400, detail="A reason is required.")
    # Record under the manager's name: the history has no actor column, so the
    # name leads the stored reason, as the approved mockup shows ("changed by ...").
    recorded_reason = f"Changed by {manager_name}: {reason}"
    try:
        return await apply_entitlement_changes(
            employee_id, body.changes, recorded_reason, None, source=MANAGER_CHANNEL,
        )
    except LookupError as e:                                   # the person disappeared mid-edit
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:                                   # blank reason / no changes / bad value
        raise HTTPException(status_code=400, detail=str(e))
    except StaffWriteError as e:                              # the staff write failed upstream
        raise HTTPException(status_code=502, detail=str(e))


# ============================
# #372 - the full record for the manager person view
# ============================

@router.get("/team/members/{employee_id}/record")
async def team_member_record(user: AuthUser, employee_id: str):
    """One teammate's full record for the manager person view (#372).

    Returns the same payload as the admin ``GET /admin/employees/{id}`` (the
    editable entitlements and balances, the resolved people fields and the change
    history), so the admin ``PersonProfile`` is reused in a manager-scoped mode
    rather than copied. Only the calling manager's own team may be read; an
    archived person is seen from the admin staff screen, not here (#119).

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate.

    Returns:
        The staff record payload (``employee_id``, ``fields``, ``people``,
        ``history``), identical in shape to the admin employee record.

    Raises:
        HTTPException: 403 for an employee token or a non-teammate (an archived
            person included), 404 when the person is gone.
    """
    _manager, manager_name = await _signed_in_manager(user)   # role check + the caller's name
    await _require_teammate(employee_id, manager_name)        # 403 unless on my team (and not archived)
    record = await get_staff_record(employee_id)              # the same payload the admin panel reads
    if record is None:                                        # vanished between the guard and the read
        raise HTTPException(status_code=404, detail="Employee not found")
    return record


# ============================
# #363 - add and remove people on the team
# ============================

@router.get("/team/addable")
async def team_addable(user: AuthUser):
    """Staff the manager may add: everyone not already on their team (#363).

    Feeds the "Add to my team" picker. Excludes the people who already list this
    manager and the manager themselves.

    Args:
        user: The signed-in manager or admin.

    Returns:
        ``{"people": [{id, name, email, department, location, employment_type,
        system_check, managers}, ...]}`` in name order. ``managers`` is the names
        already on the person, so the add-to-team module can show them (#372).
    """
    manager, _manager_name = await _signed_in_manager(user)
    all_staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)  # active people only
    people = []
    for item in all_staff:
        f = item.get("fields", {})
        if _may_add(manager["fields"], f) is not None:    # skip self, archived and already-on-team (#362)
            continue
        person = _format_employee(f, item["id"])          # the same shape the team list uses
        # Their current managers by name, so the add-to-team module can show who
        # already manages them before the manager confirms the add (#372).
        person["managers"] = [m["name"] for m in _extract_all_managers(f)]
        people.append(person)
    people.sort(key=lambda p: (p.get("name") or "").lower())  # name order, like the team cards
    return {"people": people}


@router.get("/team/members/{employee_id}/managers")
async def team_member_managers(user: AuthUser, employee_id: str):
    """One teammate's managers and whether anyone else would remain (#363).

    The UI reads this before the "Remove from my team" confirmation, so it can
    warn when the person would be left with no manager at all (their requests
    would then have nobody to go to).

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate.

    Returns:
        ``{"id", "name", "managers": [name, ...], "has_other_manager": bool}``,
        where ``has_other_manager`` is true when a manager other than the caller
        is on the person.

    Raises:
        HTTPException: 403 for a non-teammate, 404 when the person is gone.
    """
    _manager, manager_name = await _signed_in_manager(user)
    target = await _require_teammate(employee_id, manager_name)
    current = _extract_all_managers(target.get("fields", {}))    # [{sp_user_id, name}]
    others = [m for m in current if m["name"] != manager_name]   # anyone but the caller
    return {
        "id": str(employee_id),
        "name": target["fields"].get("Title", ""),
        "managers": [m["name"] for m in current],
        "has_other_manager": len(others) > 0,
    }


@router.post("/team/members/{employee_id}")
async def team_add_member(user: AuthUser, employee_id: str):
    """Add a person to the calling manager's team (#363).

    Appends the manager to the person's ``AllManagers`` without touching the other
    managers already there, through ``update_employee_managers`` (so the write
    goes through ``staff_store`` and open dashboards re-read). Recorded under the
    manager's name, channel ``manager_dashboard``. Idempotent: adding someone
    already on the team changes nothing.

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the person to add.

    Returns:
        ``{"id", "name", "managers": [...], "added": bool}``; ``added`` is false
        when they were already on the team.

    Raises:
        HTTPException: 503 when processing is off, 404 when the person is gone,
            400 (SharePoint only) when the manager has no Microsoft 365 account.
    """
    manager, manager_name = await _signed_in_manager(user)
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _refuse_if_paused()                             # same 503 as the other write routes mid-switch
    try:
        # The lock serialises this person's AllManagers read-modify-write with any
        # balance edit, so two concurrent manager changes never clobber each other.
        async with lock_manager.lock(str(employee_id)):
            target = await get_employee_by_id(employee_id)    # re-read under the lock
            if not target:
                raise HTTPException(status_code=404, detail="Employee not found")
            tfields = target.get("fields", {})
            current = _extract_all_managers(tfields)
            current_ids = [m["sp_user_id"] for m in current]
            link_id = await _manager_link_id(user, manager["fields"])   # my id in an AllManagers list
            # Already on my team is a harmless no-op (not a refusal), by name or by
            # id in case a name ever drifts from its stored lookup id.
            if link_id in current_ids or _is_in_all_managers(tfields, manager_name):
                return {
                    "id": str(employee_id),
                    "name": tfields.get("Title", ""),
                    "managers": [m["name"] for m in current],
                    "added": False,
                }
            refusal = _may_add(manager["fields"], tfields)    # self / archived are hard refusals (#362)
            if refusal is not None:
                raise HTTPException(status_code=403, detail=refusal)
            result = await update_employee_managers(          # keep the others, add me
                int(employee_id), current_ids + [link_id],
                history_source=MANAGER_CHANNEL,
                history_reason=f"Added to the team by {manager_name} from the manager dashboard.",
            )
            return {**result, "added": True}
    except SystemPaused as e:                             # the switch began mid-write: refuse cleanly
        raise HTTPException(status_code=503, detail=e.message)


@router.delete("/team/members/{employee_id}")
async def team_remove_member(user: AuthUser, employee_id: str):
    """Remove the calling manager from a teammate, leaving other managers (#363).

    Drops only this manager from the person's ``AllManagers``; anyone else managing
    them stays. Recorded under the manager's name, channel ``manager_dashboard``.
    The response says whether another manager remains, so the UI can confirm that
    an about-to-be-orphaned person's requests would have nobody to go to.

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate.

    Returns:
        ``{"id", "name", "managers": [...], "removed": true, "has_other_manager":
        bool}``.

    Raises:
        HTTPException: 403 for a non-teammate, 503 when processing is off, 404
            when the person is gone.
    """
    manager, manager_name = await _signed_in_manager(user)
    await _require_teammate(employee_id, manager_name)    # must already be on my team (and not archived)
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _refuse_if_paused()                             # same 503 as the other write routes mid-switch
    try:
        # The lock serialises this person's AllManagers read-modify-write, as the
        # add path and the balance edits do.
        async with lock_manager.lock(str(employee_id)):
            target = await get_employee_by_id(employee_id)    # re-read for the current manager set
            if not target:                                    # removed between the guard and here
                raise HTTPException(status_code=404, detail="Employee not found")
            current = _extract_all_managers(target.get("fields", {}))
            # Keep every manager that is not me. Name match (the membership check is
            # by name, and names are unique); id match too, in case a name ever drifts.
            link_id = await _manager_link_id(user, manager["fields"])
            kept = [m for m in current if m["name"] != manager_name and m["sp_user_id"] != link_id]
            result = await update_employee_managers(
                int(employee_id), [m["sp_user_id"] for m in kept],
                history_source=MANAGER_CHANNEL,
                history_reason=f"{manager_name} removed themselves as a manager from the manager dashboard.",
            )
            return {**result, "removed": True, "has_other_manager": len(kept) > 0}
    except SystemPaused as e:                             # the switch began mid-write: refuse cleanly
        raise HTTPException(status_code=503, detail=e.message)
