"""Live request events, request timelines and the build version (#328-#332).

Three things ride on the one timeline table:

- ``GET /events/stream`` is a server-sent-events feed. A dashboard opens it and
  receives each new event the moment it commits, so an approval by email, text
  or another dashboard shows on every open page within a second or two.
- ``GET /events`` is the catch-up a reader polls when the stream is blocked, and
  the replay a reconnecting stream asks for by ``Last-Event-ID``.
- ``GET .../requests/{kind}/{item_id}/timeline`` returns one request's history
  as plain lines, with the field-level change history added for admins (#332).

Who sees what is decided by role, the same way the read endpoints decide it: an
employee sees only their own request's events, a manager their team's, an admin
every one. Employees and managers prove themselves with the signed dashboard
token they already pass; an admin with the admin-dashboard session, as a header
on a normal request or, because an EventSource cannot set headers, as a query
parameter on the stream.
"""
import asyncio
import logging
import os
from dataclasses import dataclass, field

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.routes.dashboard import (
    AuthUser,
    _build_staff_lookups,
    _require_role,
    get_employee_by_id,
)
from app.services.admin_auth import SESSION_HEADER, session_is_valid, stored_password
from app.services.dashboard_tokens import validate_dashboard_token
from app.services.event_bus import event_bus, fetch_events, sse_message
from app.services.request_live_reads import WIRE_TO_STORED

logger = logging.getLogger(__name__)
router = APIRouter()

# How often the open stream sends a comment line when nothing else is flowing,
# so a proxy keeps the connection and the client's dead-stream timer resets.
HEARTBEAT_SECONDS = 15

# Headers that stop Railway's proxy (and nginx) buffering a streamed response.
STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",          # nginx/Railway: do not buffer this response
    "Connection": "keep-alive",
}

# One request kind the url can name, mapped to the stored kind for a lookup.
# Accepts both the url form ("carryover-payout") and the stored form.
_KIND_ALIASES = {**WIRE_TO_STORED, "carryover_payout": "carryover_payout"}

# How each event type reads at the start of a timeline line.
_TYPE_VERB = {
    "submitted": "Submitted",
    "sent_to_manager": "Sent to the manager",
    "receipt_sent": "Receipt sent",
    "link_opened": "Manager opened the link",
    "approved": "Approved",
    "rejected": "Rejected",
    "refused": "Action refused",
    "balance_applied": "Balance updated",
    "counted": "Vacation counted",
    "no_balance_used": "No balance used",
    "result_sent": "Result sent",
    "send_failed": "A message failed to send",
    "reminder_sent": "Reminder sent",
    "refunded": "Refunded",
    "balance_reversed": "Balance reversed",
    "edited": "Edited",
    "cancelled": "Cancelled",
    "imported": "Imported",
    "flagged": "Flagged",
}

# How each channel reads in the middle of a timeline line.
_CHANNEL_PHRASE = {
    "request_page": "on the request page",
    "form": "by form",
    "email_link": "by email",
    "sms": "by text",
    "employee_dashboard": "on the dashboard",
    "manager_dashboard": "on the dashboard",
    "admin_dashboard": "on the admin dashboard",
    "import": "from SharePoint",
    "sharepoint_check": "from a SharePoint check",
}


def app_version() -> str:
    """The running build's version, for the auto-update check (#331).

    Prefers an explicit ``APP_VERSION``; falls back to the git sha Railway
    injects, then to ``"dev"`` so local runs always have a value.

    Returns:
        A short version string.
    """
    return os.environ.get("APP_VERSION") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "dev"


@dataclass
class _Viewer:
    """Who is reading the stream, and the rows they may see."""

    role: str                                        # "employee", "manager" or "admin"
    employee_id: str | None = None                   # the employee's own id (employee view)
    manager_id: str | None = None                    # the manager's own id (manager view)
    team_ids: set[str] = field(default_factory=set)  # the manager's team, as Staff Directory ids


def _event_visible(payload: dict, viewer: _Viewer) -> bool:
    """Whether one event payload should reach this viewer.

    Args:
        payload: An event payload from the bus or the catch-up read.
        viewer: The resolved reader.

    Returns:
        True when the viewer's role and ids allow it.
    """
    if viewer.role == "admin":
        return True                                            # admin sees every request
    if viewer.role == "employee":
        return str(payload.get("employee_id")) == str(viewer.employee_id)
    if viewer.role == "manager":
        if viewer.manager_id and str(payload.get("manager_id")) == str(viewer.manager_id):
            return True                                        # a request routed to this manager
        return str(payload.get("employee_id")) in viewer.team_ids   # or one of the manager's people
    return False


async def _team_ids_for(manager_uid: str) -> tuple[str, set[str]]:
    """A manager's own id and their team's Staff Directory ids, the way /team decides.

    Args:
        manager_uid: The manager's Staff Directory id (from the signed token).

    Returns:
        (manager_id, team_ids). team_ids is empty when nothing is managed.
    """
    manager = await get_employee_by_id(manager_uid)
    manager_name = manager["fields"].get("Title", "") if manager else ""
    staff_by_name, _by_id, _names, mgr_to_emp_names = await _build_staff_lookups()
    team_names = mgr_to_emp_names.get(manager_name, set())
    team_ids = {
        str(staff_by_name[nm.strip().lower()]["id"])
        for nm in team_names
        if staff_by_name.get(nm.strip().lower(), {}).get("id") is not None
    }
    return str(manager_uid), team_ids


async def _resolve_viewer(
    request: Request,
    token: str | None,
    role: str | None,
    uid: str | None,
    exp: str | None,
    admin_session: str | None,
) -> _Viewer:
    """Authenticate a stream or catch-up request and return its viewer.

    Employees and managers use the signed dashboard token (query parameters);
    an admin uses the admin-dashboard session, as the ``X-Admin-Session`` header
    or, for an EventSource that cannot set one, an ``admin_session`` query value.

    Args:
        request: The incoming request (for the admin session header).
        token: The signed dashboard token, or None.
        role: The token's role, or None.
        uid: The token's user id, or None.
        exp: The token's expiry, or None.
        admin_session: The admin session passed as a query value, or None.

    Returns:
        The resolved viewer.

    Raises:
        HTTPException: 401 when nothing authenticates the caller.
    """
    if token and role and uid and exp:
        valid, _msg = validate_dashboard_token(role, uid, token, exp)
        if valid and role == "employee":
            return _Viewer("employee", employee_id=uid)
        if valid and role == "manager":
            manager_id, team_ids = await _team_ids_for(uid)
            return _Viewer("manager", manager_id=manager_id, team_ids=team_ids)
        if valid and role == "admin":
            return _Viewer("admin")
    candidate = request.headers.get(SESSION_HEADER, "") or (admin_session or "")
    if candidate and session_is_valid(candidate, await stored_password()):
        return _Viewer("admin")
    raise HTTPException(status_code=401, detail="Sign in to see live updates.")


async def _stream(viewer: _Viewer, start_after: int):
    """Yield the SSE text for one open connection: replay, then live, with heartbeats.

    Args:
        viewer: The resolved reader.
        start_after: Replay events with an id greater than this first.

    Yields:
        SSE message blocks and heartbeat comment lines.
    """
    queue = event_bus.subscribe()                              # start buffering live events at once
    try:
        last_sent = start_after
        for payload in await fetch_events(start_after):        # replay what the reader missed
            if _event_visible(payload, viewer):
                yield sse_message(payload)
            last_sent = max(last_sent, int(payload["id"]))
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"                         # keep the connection and the proxy alive
                continue
            if int(payload["id"]) <= last_sent:
                continue                                       # already replayed: do not repeat
            if _event_visible(payload, viewer):
                yield sse_message(payload)
            last_sent = max(last_sent, int(payload["id"]))
    finally:
        event_bus.unsubscribe(queue)                           # drop the queue when the client goes


@router.get("/events/stream")
async def events_stream(
    request: Request,
    token: str | None = Query(None),
    role: str | None = Query(None),
    uid: str | None = Query(None),
    exp: str | None = Query(None),
    admin_session: str | None = Query(None),
    after: int = Query(0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    """Open the live server-sent-events feed of request timeline events.

    Args:
        request: The incoming request.
        token, role, uid, exp: The signed dashboard token (employee/manager).
        admin_session: The admin session, for an EventSource that cannot set a header.
        after: Replay from this event id when there is no Last-Event-ID.
        last_event_id: The browser's resume point after a dropped connection.

    Returns:
        A streaming ``text/event-stream`` response.
    """
    viewer = await _resolve_viewer(request, token, role, uid, exp, admin_session)
    start_after = after
    if last_event_id and last_event_id.isdigit():
        start_after = int(last_event_id)                       # the browser's resume wins over ?after
    return StreamingResponse(
        _stream(viewer, start_after),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


@router.get("/events")
async def events_catch_up(
    request: Request,
    token: str | None = Query(None),
    role: str | None = Query(None),
    uid: str | None = Query(None),
    exp: str | None = Query(None),
    admin_session: str | None = Query(None),
    after: int = Query(0),
):
    """The polling fallback: every event the viewer may see after an id.

    Args:
        request: The incoming request.
        token, role, uid, exp: The signed dashboard token (employee/manager).
        admin_session: The admin session as a query value.
        after: Return events with an id greater than this.

    Returns:
        {"events": [...], "version": "<build>"}.
    """
    viewer = await _resolve_viewer(request, token, role, uid, exp, admin_session)
    payloads = await fetch_events(int(after))
    return {"events": [p for p in payloads if _event_visible(p, viewer)], "version": app_version()}


@router.get("/version")
async def version():
    """The running build's version, polled by the frontend's auto-update (#331).

    Returns:
        {"version": "<build>"}.
    """
    return {"version": app_version()}


def _timeline_line(event) -> str:
    """One plain-words line for a timeline event, e.g. "Approved by email by Dave".

    Args:
        event: A RequestEvent row.

    Returns:
        The line text (the time is shown separately by the reader).
    """
    parts = [_TYPE_VERB.get(event.type, event.type)]           # the verb
    phrase = _CHANNEL_PHRASE.get(event.channel)
    if phrase:
        parts.append(phrase)                                   # where it came from
    elif event.channel == "system" and not event.actor:
        parts.append("automatically")                          # a background step with no person
    if event.actor:
        parts.append(f"by {event.actor}")                      # who did it
    return " ".join(parts)


async def _load_timeline(stored_kind: str, item_id: int):
    """The request row, its events and its field changes, for a timeline.

    Args:
        stored_kind: The stored request kind.
        item_id: The item_id (the number people see).

    Returns:
        (request, events, changes); request is None when there is no such item.
    """
    from sqlalchemy import select

    from app.database import async_session
    from app.models.request_change import RequestChange
    from app.models.request_event import RequestEvent
    from app.models.request_record import RequestRecord

    async with async_session() as session:
        request = (await session.execute(
            select(RequestRecord).where(
                RequestRecord.kind == stored_kind, RequestRecord.item_id == item_id
            )
        )).scalars().first()
        if request is None:
            return None, [], []
        events = (await session.execute(
            select(RequestEvent).where(RequestEvent.request_id == request.id).order_by(RequestEvent.id)
        )).scalars().all()
        changes = (await session.execute(
            select(RequestChange).where(RequestChange.request_id == request.id).order_by(RequestChange.changed_at)
        )).scalars().all()
    return request, events, changes


def _timeline_payload(events, changes, *, with_changes: bool) -> dict:
    """Shape a timeline response: event lines, and field history for admins.

    Args:
        events: The RequestEvent rows, oldest first.
        changes: The RequestChange rows, oldest first.
        with_changes: Include the field history (admins only).

    Returns:
        {"events": [...], "changes": [...]}; changes is empty when not asked for.
    """
    event_lines = [
        {
            "id": e.id,
            "type": e.type,
            "channel": e.channel,
            "actor": e.actor,
            "at": e.created_at.isoformat() if e.created_at else None,
            "line": _timeline_line(e),
        }
        for e in events
    ]
    change_lines = []
    if with_changes:
        change_lines = [
            {
                "field": c.field,
                "old": c.old_value,
                "new": c.new_value,
                "actor": c.actor,
                "channel": c.channel,
                "at": c.changed_at.isoformat() if c.changed_at else None,
            }
            for c in changes
        ]
    return {"events": event_lines, "changes": change_lines}


def _stored_kind(kind: str) -> str:
    """Map a url or stored kind to the stored kind, or 404 on an unknown one.

    Args:
        kind: "leave", "overtime", "carryover-payout" or "carryover_payout".

    Returns:
        The stored kind.

    Raises:
        HTTPException: 404 for an unknown kind.
    """
    stored = _KIND_ALIASES.get(kind)
    if stored is None:
        raise HTTPException(status_code=404, detail="Unknown request kind")
    return stored


@router.get("/admin/requests/{kind}/{item_id}/timeline")
async def admin_request_timeline(kind: str, item_id: int):
    """One request's timeline and field history, for the admin dashboard (#332).

    Behind the admin password like every other /admin/* route (the guard in
    main.py checks the /admin path).

    Args:
        kind: The request kind (url or stored form).
        item_id: The item_id.

    Returns:
        {"events": [...], "changes": [...]}.

    Raises:
        HTTPException: 404 when there is no such request.
    """
    request, events, changes = await _load_timeline(_stored_kind(kind), item_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return _timeline_payload(events, changes, with_changes=True)


@router.get("/me/requests/{kind}/{item_id}/timeline")
async def my_request_timeline(user: AuthUser, kind: str, item_id: int):
    """One of the signed-in employee's own request timelines (events only).

    Args:
        user: From the signed token; uid is the employee's Staff Directory id.
        kind: The request kind (url or stored form).
        item_id: The item_id.

    Returns:
        {"events": [...], "changes": []}.

    Raises:
        HTTPException: 404 when there is no such request of theirs.
    """
    request, events, _changes = await _load_timeline(_stored_kind(kind), item_id)
    if request is None or str(request.employee_id) != str(user.user_id):
        raise HTTPException(status_code=404, detail="Request not found")
    return _timeline_payload(events, [], with_changes=False)


@router.get("/team/requests/{kind}/{item_id}/timeline")
async def team_request_timeline(user: AuthUser, kind: str, item_id: int):
    """One of the manager's team's request timelines (events only).

    Args:
        user: From the signed token; uid is the manager's Staff Directory id.
        kind: The request kind (url or stored form).
        item_id: The item_id.

    Returns:
        {"events": [...], "changes": []}.

    Raises:
        HTTPException: 403 for a non-manager, 404 when the request is not on their team.
    """
    _require_role(user, "manager", "admin")
    request, events, _changes = await _load_timeline(_stored_kind(kind), item_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    manager_id, team_ids = await _team_ids_for(user.user_id)
    on_team = str(request.employee_id) in team_ids or (
        request.manager_id is not None and str(request.manager_id) == manager_id
    )
    if not on_team:
        raise HTTPException(status_code=404, detail="Request not found")
    return _timeline_payload(events, [], with_changes=False)
