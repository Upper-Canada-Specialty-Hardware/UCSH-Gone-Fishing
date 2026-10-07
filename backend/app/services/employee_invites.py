"""Give a new employee access to the SharePoint site, from Add Employee (#133).

Until now IT added each new hire to the site's members by hand. Add Employee
now does it through Microsoft Graph, so IT only creates the person's mailbox
and Entra account:

1. Look the person up in the tenant by email (GET /users).
2. Not there (a personal or other-company address): send a guest invite
   (POST /invitations). The email Microsoft sends them links to
   INVITE_REDIRECT_URL, the request page by default.
3. Add them to the Entra security group that sits inside the site's Members
   (POST /groups/{id}/members/$ref). Graph cannot edit SharePoint groups
   directly, so that one group is placed in Members once, by an admin, and
   membership is managed here.

Every step is recorded in employee_invites and never raises: a failed invite
must not stop the employee record from being created. The whole thing stays
off until INVITES_ENABLED is set, after IT has granted the app User.Read.All,
User.Invite.All and GroupMember.ReadWrite.All and created the group.
"""

import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.graph.client import GRAPH_BASE, graph_client
from app.models import EmployeeInvite
from app.models.mixins import utcnow

logger = logging.getLogger(__name__)


def _odata_quote(value: str) -> str:
    """Quote a string for an OData $filter, doubling any single quote.

    Args:
        value: The raw value, e.g. an email.

    Returns:
        The value wrapped in single quotes, safe inside $filter.
    """
    return "'" + value.replace("'", "''") + "'"


def _redirect_url() -> str:
    """Where the guest lands after accepting the invite.

    Returns:
        INVITE_REDIRECT_URL, else the request page, else the SharePoint site.
    """
    if settings.INVITE_REDIRECT_URL:
        return settings.INVITE_REDIRECT_URL
    if settings.DASHBOARD_FRONTEND_URL:
        return settings.DASHBOARD_FRONTEND_URL.rstrip("/") + "/#/request"
    return f"https://{settings.SP_SITE_HOST}{settings.SP_SITE_PATH}"


def _graph_error(e: Exception) -> str:
    """A short readable reason from a Graph failure.

    Args:
        e: What the Graph call raised.

    Returns:
        Graph's own error message when there is one, else the exception text.
    """
    if isinstance(e, httpx.HTTPStatusError):
        try:
            message = e.response.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        return f"Microsoft 365 said {e.response.status_code}: {message or e.response.text[:200]}"
    return str(e)[:300]


async def find_user_id(email: str) -> str | None:
    """The Entra object id of a user or guest with this email, if any.

    Args:
        email: The address.

    Returns:
        The object id, or None when nobody in the tenant has it.
    """
    quoted = _odata_quote(email.strip().lower())
    data = await graph_client.get("/users", params={
        "$filter": f"mail eq {quoted} or userPrincipalName eq {quoted}",
        "$select": "id,userType,mail",
    })
    users = data.get("value", [])
    return users[0]["id"] if users else None


async def send_guest_invite(email: str, name: str) -> str:
    """Invite someone from outside the tenant as a guest.

    Args:
        email: Their address.
        name: Display name for the guest account.

    Returns:
        The new guest's object id.
    """
    data = await graph_client.post("/invitations", json={
        "invitedUserEmailAddress": email,
        "invitedUserDisplayName": name,
        "inviteRedirectUrl": _redirect_url(),
        "sendInvitationMessage": True,                         # Microsoft emails them the accept link
        "invitedUserMessageInfo": {
            "customizedMessageBody": (
                "You have been added to UCSH Out of Office, where you request leave, "
                "overtime and carry-over. Accept this invite to get access."
            ),
        },
    })
    return data["invitedUser"]["id"]


async def add_to_site_members(user_id: str) -> bool:
    """Add a user to the group inside the site's Members.

    Args:
        user_id: Entra object id.

    Returns:
        True when added now; False when they were already a member.

    Raises:
        httpx.HTTPStatusError: Any other Graph refusal.
    """
    try:
        await graph_client.post(
            f"/groups/{settings.SITE_MEMBERS_GROUP_ID}/members/$ref",
            json={"@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"},
        )
        return True
    except httpx.HTTPStatusError as e:
        # Graph answers 400 "added object references already exist" for a member.
        if e.response.status_code == 400 and "already exist" in e.response.text:
            return False
        raise


async def invite_employee(employee_id: str, email: str, name: str) -> dict:
    """Make sure a new employee can open the site; record and return the outcome.

    Never raises: every failure is recorded and returned.

    Args:
        employee_id: Their Staff Directory id.
        email: Their email.
        name: Their name.

    Returns:
        {"status", "detail", "group_added", "user_id"}; status is
        "in_tenant", "invited", "skipped" or "failed".
    """
    email = (email or "").strip().lower()
    result = {"status": "failed", "detail": "", "group_added": False, "user_id": None}
    if not settings.INVITES_ENABLED:
        result.update(status="skipped", detail="Site invites are turned off; IT adds them to the site.")
    elif not settings.SITE_MEMBERS_GROUP_ID:
        result.update(detail="No site members group is configured (SITE_MEMBERS_GROUP_ID).")
    elif not email:
        result.update(detail="The record has no email address.")
    else:
        try:
            user_id = await find_user_id(email)
            if user_id:
                result.update(status="in_tenant", user_id=user_id)
            else:
                user_id = await send_guest_invite(email, name)
                result.update(status="invited", user_id=user_id)
            added = await add_to_site_members(user_id)
            result["group_added"] = True
            if result["status"] == "invited":
                result["detail"] = f"Invite emailed to {email}; they get access once they accept it."
            else:
                result["detail"] = ("Added to the site members." if added
                                    else "Already a site member.")
        except Exception as e:  # noqa: BLE001 - recorded; never blocks the new employee
            logger.exception("Site invite failed for employee #%s", employee_id)
            result.update(status="failed", detail=_graph_error(e))

    await _record(employee_id, email, name, result)
    return result


async def _record(employee_id: str, email: str, name: str, result: dict) -> None:
    """Upsert the employee's invite row; a database error is logged only."""
    try:
        async with async_session() as session:
            row = await session.scalar(
                select(EmployeeInvite).where(EmployeeInvite.employee_id == str(employee_id))
            )
            if row is None:
                row = EmployeeInvite(employee_id=str(employee_id), attempts=0, created_at=utcnow())
                session.add(row)
            row.email, row.name = email, name
            row.status, row.detail = result["status"], result["detail"]
            row.group_added = result["group_added"]
            row.user_id = result["user_id"] or row.user_id             # keep a known id on a later failure
            if result["status"] in ("in_tenant", "invited"):
                row.attempts = (row.attempts or 0) + 1
            row.updated_at = utcnow()
            await session.commit()
    except Exception:  # noqa: BLE001 - the invite outcome is already returned
        logger.exception("Could not record the site invite for employee #%s", employee_id)


async def list_invites() -> list[dict]:
    """Every recorded invite, newest first, for the admin dashboard.

    Returns:
        Plain dicts, snake_case.
    """
    async with async_session() as session:
        rows = (await session.scalars(
            select(EmployeeInvite).order_by(EmployeeInvite.updated_at.desc())
        )).all()
    return [
        {
            "employee_id": r.employee_id, "email": r.email, "name": r.name,
            "status": r.status, "group_added": r.group_added, "detail": r.detail,
            "attempts": r.attempts, "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
