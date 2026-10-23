"""Manager control over their own team (#362, #363).

A manager gets admin-level control, but only over the people who list them in
their ``AllManagers`` field. Four things live here, apart from the read-only
``/team/*`` endpoints in ``routes/dashboard.py``:

- change a teammate's balances and entitlements, with a required reason, through
  the same service the admin person panel uses (``staff_entitlements``), so the
  validation and the live-update notify are identical (#362);
- add a person to the manager's own team and remove the manager from a person's
  team, through ``manager_assignments.update_employee_managers`` so the write
  goes through ``staff_store`` and open dashboards re-read (#363);
- tell the UI which staff a manager may still add, and whether removing
  themselves would leave a person with no manager at all;
- Potential Employee Setup Issues for the team: the admin setup list, the
  single-person check and Settle requests, reaching teammates only (#463).

Every change is recorded under the manager's name, channel ``manager_dashboard``,
exactly as an admin change is recorded under ``admin_dashboard``. All of it is
token-authenticated through the shared ``AuthUser`` and refused (403) for anyone
not on the calling manager's team.
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
# The read-side helpers live in the dashboard router; reuse them so team
# membership is decided one way everywhere (the brief says to use these).
from app.services.manager_match import manager_name_of, normalise_name  # who manages whom (#376)
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
from app.database import async_session
from app.services import balance_settle  # settle a teammate's unmoved request (#463)
from app.services.concurrency import lock_manager  # serialise the AllManagers read-modify-write
from app.services.employee import get_employee_by_id
from app.services.employee_validation import validate_all_employee_setups, validate_employee_setup
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.request_decisions import _load as _load_request  # whose request it is (#463)
from app.services.manager_assignments import _extract_all_managers, update_employee_managers
from app.services.staff_entitlements import StaffWriteError, apply_entitlement_changes, get_staff_record
from app.services.switch_pause import SystemPaused  # the mid-switch write refusal -> 503

router = APIRouter()
logger = logging.getLogger(__name__)

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
    # The name carries the manager's link ids, so membership matches by id or name (#376).
    return manager, await manager_name_of(manager)


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
    if not person_name or normalise_name(person_name) == normalise_name(manager_name):  # not yourself
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
    """Body for the team profile PATCH: new balances and/or directory fields, and a reason.

    A manager edits every field of a teammate that an admin can edit in the admin
    staff profile, balances and the directory fields (email, placement, ...)
    alike, with no restriction; the only difference from an admin is whose records
    they may edit (their own team only) (#432). So this mirrors the admin
    ``EntitlementChanges`` body: both a ``changes`` map and a ``fields`` map.

    Attributes:
        changes: snake_case wire key -> new numeric value (see
            ``staff_entitlements.EDITABLE``); empty when only fields change.
        fields: snake_case wire key -> new value for the Staff Directory text,
            choice and person fields (see ``staff_entitlements.TEXT_FIELDS`` /
            ``PERSON_FIELDS`` and the managers key); empty when only pots change.
        reason: Why the manager made the change; required and recorded.
    """
    # ``Any`` not ``float``: the service's ``_as_number`` does the real
    # type-checking and rejects non-numbers with a message written for the UI.
    changes: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    reason: str


@router.patch("/team/members/{employee_id}/entitlements")
async def team_update_entitlements(user: AuthUser, employee_id: str, body: TeamEntitlementChanges):
    """Edit one teammate's full profile (balances and directory fields), with a reason (#432).

    The manager-scoped twin of the admin person panel's save: it reuses the admin
    service (``apply_entitlement_changes``) with both the balance pots and the
    directory fields, so the same validation, the same per-employee lock and the
    same ``staff_store`` write (and its live-update notify) all apply. A manager
    edits every field an admin can, with no restriction; the change is recorded
    under the manager's name, channel ``manager_dashboard``. Only the manager's
    own team may be edited, and never the manager's own record.

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate being edited.
        body: The new balance values, the new directory fields, and the reason.

    Returns:
        The service result: what changed and whether the history was recorded.

    Raises:
        HTTPException: 403 for a non-teammate or the manager's own record, 503
            when processing is off, 400 on a blank reason or bad value, 404 when
            the person is gone, 502 on a write failure.
    """
    manager, manager_name = await _signed_in_manager(user)   # role + the caller's name
    if str(employee_id) == str(user.user_id):                 # a manager cannot edit their own record
        raise HTTPException(status_code=403, detail="You cannot edit your own record.")
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
        # Pass the directory fields too (#432): the admin save does the same, so a
        # manager's edit to email, placement or any field runs the identical plan,
        # write and history path as the admin's, only under the manager's channel.
        return await apply_entitlement_changes(
            employee_id, body.changes, recorded_reason, body.fields, source=MANAGER_CHANNEL,
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


# ============================
# #463 - Potential Employee Setup Issues, for the manager's own team
# ============================
# The admin screen's twin, limited to the people who list the calling manager.
# The same checks and the same settle service run; only whose records they reach
# differs, as with the profile edit above.

class TeamSettleBody(BaseModel):
    """What one step of the settle dialog posts, from the manager dashboard."""

    outcome: str                      # "now", "hand" or "none"
    reason: str                       # required, never blank
    pieces: dict[str, float] | None = None   # "hand" leave only: {pot: days taken}


def _settle_kind_for(request_type: str) -> str:
    """The stored request kind for a url request type, or a 400.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".

    Returns:
        The stored kind the settle service reads.

    Raises:
        HTTPException: 400 for an unknown type.
    """
    from app.routes.admin_requests import _settle_kind   # one mapping for both screens; late, to keep router imports apart
    return _settle_kind(request_type)


# One answer for a request that is missing or not on the team, so a manager
# cannot probe whether someone else's request exists or what state it is in.
REQUEST_NOT_ON_TEAM = "That request was not found on your team."


async def _require_team_member(user: DashboardUser, employee_id: str, manager_name: str) -> dict:
    """Confirm a person is on the manager's team and is not the manager.

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the person.
        manager_name: The calling manager's display name.

    Returns:
        The person's ``{"id", "fields"}`` staff item.

    Raises:
        HTTPException: 403 for the manager's own record (a manager does not
            check or settle their own, as with the profile edit) or a
            non-teammate, 404 when the person is gone.
    """
    if str(employee_id) == str(user.user_id):            # never their own record
        raise HTTPException(status_code=403, detail="You cannot check or settle your own record.")
    return await _require_teammate(employee_id, manager_name)   # 403 unless on my team


async def _request_owner(kind: str, request_id: str) -> str | None:
    """Whose request this is: the staff id on the stored request, or None.

    A plain row read, done before the settle service runs, so the team check
    comes first and nothing about another person's request is revealed.

    Args:
        kind: The stored request kind.
        request_id: The request's item id.

    Returns:
        The owner's staff id, or None when there is no such request.
    """
    async with async_session() as session:
        request = await _load_request(session, kind, request_id)   # None for a bad or unknown id
        return str(request.employee_id) if request is not None else None


async def _require_teammate_request(user: DashboardUser, kind: str, request_id: str, manager_name: str) -> None:
    """Confirm a request belongs to one of the manager's teammates, before any settle runs.

    Args:
        user: The signed-in manager or admin.
        kind: The stored request kind.
        request_id: The request's item id.
        manager_name: The calling manager's display name.

    Raises:
        HTTPException: 403 for the manager's own request; 404 with one message
            for a request that is missing or belongs to someone not on the team.
    """
    owner = await _request_owner(kind, request_id)
    if owner is None:                                    # no such request
        raise HTTPException(status_code=404, detail=REQUEST_NOT_ON_TEAM)
    if owner == str(user.user_id):                       # their own request: refused like their own record
        raise HTTPException(status_code=403, detail="You cannot check or settle your own record.")
    try:
        await _require_teammate(owner, manager_name)     # 403 / 404 when not on my team
    except HTTPException:
        raise HTTPException(status_code=404, detail=REQUEST_NOT_ON_TEAM)   # same answer as a missing one


@router.get("/team/employee-setup")
async def team_employee_setup(user: AuthUser):
    """Everyone on the manager's team whose staff record has a setup problem (#463).

    The same sweep as ``/admin/employee-setup``, graded only for the people who
    list this manager, not archived, and never the manager's own record (a
    manager does not edit their own record). Read-only, so not gated on
    processing.

    Args:
        user: The signed-in manager or admin.

    Returns:
        ``{"total_checked", "flagged", "directory_unreadable"}`` for the team.

    Raises:
        HTTPException: 403 for an employee token, 500 when the sweep fails.
    """
    _manager, manager_name = await _signed_in_manager(user)

    def on_my_team(record: dict) -> bool:
        """Whether a staff record is one of this manager's teammates."""
        fields = record.get("fields", {})
        return (
            str(record.get("id", "")) != str(user.user_id)     # never their own record
            and not is_archived(fields)                      # archived people are off every team
            and _is_in_all_managers(fields, manager_name)    # lists me as a manager
        )

    try:
        return await validate_all_employee_setups(keep=on_my_team)
    except Exception:  # noqa: BLE001 - one message for any failure, details in the log
        logger.exception("Team employee setup sweep failed for manager %s", user.user_id)
        raise HTTPException(status_code=500, detail="Setup check failed - check server logs")


@router.get("/team/validate-employee/{employee_id}")
async def team_validate_employee(user: AuthUser, employee_id: str):
    """The single-person setup check, for one of the manager's teammates (#463).

    Args:
        user: The signed-in manager or admin.
        employee_id: Staff Directory item id of the teammate.

    Returns:
        The same report as ``/admin/validate-employee``.

    Raises:
        HTTPException: 403 for a non-teammate, 404 when the person is gone,
            500 when the check fails.
    """
    _manager, manager_name = await _signed_in_manager(user)
    await _require_team_member(user, employee_id, manager_name)   # 403 for themselves or a non-teammate
    try:
        return await validate_employee_setup(employee_id)
    except Exception:  # noqa: BLE001 - one message for any failure, details in the log
        logger.exception("Team employee validation failed for #%s", employee_id)
        raise HTTPException(status_code=500, detail="Validation failed - check server logs")


@router.get("/team/requests/{request_type}/{request_id}/settle")
async def team_settle_preview(user: AuthUser, request_type: str, request_id: str):
    """What settling a teammate's approved request would do, read only (#463).

    Args:
        user: The signed-in manager or admin.
        request_type: "leave", "overtime" or "carryover-payout".
        request_id: The request's item id.

    Returns:
        The same preview as the admin route.

    Raises:
        HTTPException: 400 bad type, 403 their own request, 404 missing or not
            on the team, 409 not settleable.
    """
    _manager, manager_name = await _signed_in_manager(user)
    kind = _settle_kind_for(request_type)
    await _require_teammate_request(user, kind, request_id, manager_name)   # before the settle service reads anything
    try:
        return await balance_settle.settle_preview(kind, request_id)
    except balance_settle.SettleError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/team/requests/{request_type}/{request_id}/settle")
async def team_settle_request(user: AuthUser, request_type: str, request_id: str, body: TeamSettleBody):
    """Settle a teammate's approved request that never moved a balance (#463).

    The admin route's twin: the same service, so the same checks and ledger
    rows. The reason is stored under the manager's name.

    Args:
        user: The signed-in manager or admin.
        request_type: "leave", "overtime" or "carryover-payout".
        request_id: The request's item id.
        body: outcome ("now", "hand", "none"), the reason, and for a "hand"
            leave the days taken per pot.

    Returns:
        ``{"status": "settled", "outcome", "balance_state", "moves"}``.

    Raises:
        HTTPException: 400 bad input, 403 their own request, 404 missing or not
            on the team, 409 already settled or not possible, 503 while
            processing is off.
    """
    _manager, manager_name = await _signed_in_manager(user)
    kind = _settle_kind_for(request_type)
    if not settings.PROCESSING_ENABLED:                   # a balance write, like the admin route
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _require_teammate_request(user, kind, request_id, manager_name)   # before anything is read or written
    reason = (body.reason or "").strip()
    if not reason:                                        # say it plainly before the service does
        raise HTTPException(status_code=400, detail="A reason is required.")
    try:
        result = await balance_settle.settle(
            kind, request_id, body.outcome, f"Settled by {manager_name}: {reason}", body.pieces,
        )
    except balance_settle.SettleError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    logger.info("Manager %s settled %s request #%s: %s", user.user_id, request_type, request_id, result["outcome"])
    return result
