"""The signed status token and progress read for a request page submission (#364).

When a signed-in employee sends a request from the public request page
(routes/self_service.py), the page shows "what happens next": sent to the
manager, approved or rejected, the balance applied, and the result email. These
helpers let that page follow its own request without a sign-in:

* ``status_token_for`` mints a signed token naming one request (its kind and
  item_id); the submit response hands it to the page.
* ``request_ref_from_status_token`` checks a token and returns the request it
  names, or None for a forged or malformed one.
* ``request_progress`` reads that one request's row and its timeline events and
  returns only the few booleans and times the steps need.

The token is an HMAC over the request's kind and item_id with
``APPROVAL_LINK_SECRET``, the same approach as the approval links, so no
other request can be asked about and the token cannot be forged without the
secret. The progress read works on the Postgres ``requests`` table; on the
SharePoint path there is no row to follow, so no token is minted and the page
keeps the plain steps.
"""
import hashlib
import hmac

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.request_event import RequestEvent
from app.models.request_record import RequestRecord
from app.models.request_vocab import KINDS
from app.services.request_decisions import _decider_name
from app.services.request_live_reads import WIRE_TO_STORED

# A manager decided: the event that marks the "decided" step done.
_DECISION_EVENTS = ("approved", "rejected")
# How the pots moved on approval: the event that marks the "balance" step done.
_BALANCE_EVENTS = ("balance_applied", "counted", "no_balance_used")
# The plain effect each balance event stands for, on the wire.
_BALANCE_EFFECT = {
    "balance_applied": "applied",          # pots were spent or credited
    "counted": "counted",                  # hourly vacation added to the counter
    "no_balance_used": "no_balance_used",  # bereavement, jury duty or hourly overtime
}


async def _decided_by_label(session, request: RequestRecord) -> str | None:
    """Name who decided a request, for the request page's decided step (#371).

    An admin decision or refund (actor ``admin`` or the ``admin_dashboard``
    channel) reads as "an admin"; a manager decision reads as the manager who
    actually acted, which is a stand-in when the assigned manager was off (via
    ``request_decisions._decider_name``) and the assigned manager otherwise. A
    system auto-rejection names no one. Returns None when the request is not
    decided or the decider cannot be named, and the page then shows no name.

    Args:
        session: The open read session (reused for the staff name lookup).
        request: The request row, with ``decided_by`` / ``decided_channel`` set.

    Returns:
        The decider label ("an admin" or a staff name), or None.
    """
    actor = (request.decided_by or "").strip()
    channel = request.decided_channel or ""
    if not actor and not channel:
        return None                                            # not decided yet: no one to name
    if actor.lower() == "admin" or channel == "admin_dashboard":
        return "an admin"                                      # an admin acted, not the manager (finding 10)
    if actor.lower() == "system":
        return None                                            # an auto-rejection has no human decider
    name = await _decider_name(session, request)               # a stand-in manager, when not the assigned one
    return name or request.manager_name or None                # else the assigned manager


def _sign(kind: str, item_id: int) -> str:
    """The HMAC signature over one request's kind and item_id.

    Args:
        kind: The stored request kind.
        item_id: The request's item_id.

    Returns:
        The hex signature.
    """
    message = f"request-status:{kind}:{item_id}".encode()      # own prefix: never valid as another token
    return hmac.new(settings.APPROVAL_LINK_SECRET.encode(), message, hashlib.sha256).hexdigest()


def request_status_token(kind: str, item_id: int) -> str:
    """A signed token that lets the request page follow one request's progress.

    Args:
        kind: The stored request kind (leave / overtime / carryover_payout).
        item_id: The request's item_id (the number people and links use).

    Returns:
        ``"<kind>:<item_id>.<signature>"``: the request named, plus an HMAC over it.
    """
    return f"{kind}:{item_id}.{_sign(kind, item_id)}"           # id part, then its signature


def status_token_for(request_type: str, item_id) -> str | None:
    """Mint a status token for a just-created request, or None when it cannot be named.

    Called from the submit response with the url-form request type and the new
    item's id. Returns None (so no token is sent and the page keeps the plain
    steps) when the kind is unknown or the id is not a plain number, which is the
    case on the SharePoint path, where there is no Postgres row to follow.

    Args:
        request_type: The url-form kind ("leave", "overtime", "carryover-payout").
        item_id: The created item's id, as the submit result carries it.

    Returns:
        The token, or None.
    """
    stored_kind = WIRE_TO_STORED.get(request_type)             # url form -> stored kind
    item_text = str(item_id if item_id is not None else "")    # the id as text, "" when missing
    if stored_kind is None or not item_text.isdigit():
        return None                                            # cannot name a Postgres request: no token
    return request_status_token(stored_kind, int(item_text))


def request_ref_from_status_token(token: str) -> tuple[str, int] | None:
    """The request a status token names, when its signature checks out.

    Args:
        token: A token from ``request_status_token``.

    Returns:
        ``(kind, item_id)``, or None for a malformed or forged token.
    """
    head, dot, _signature = (token or "").rpartition(".")      # "<kind>:<item_id>" . "<signature>"
    if not dot:
        return None                                            # no signature part: not a token
    kind, colon, item_part = head.partition(":")               # split the named request apart
    if not colon or kind not in KINDS or not item_part.isdigit():
        return None                                            # not a well-formed reference
    item_id = int(item_part)
    # Constant-time compare, so the signature cannot be guessed one byte at a time.
    return (kind, item_id) if hmac.compare_digest(token, request_status_token(kind, item_id)) else None


async def request_progress(kind: str, item_id: int) -> dict | None:
    """Where one request stands, in the few facts the request page's steps need.

    Reads the request row and its timeline events (``request_events``, written by
    ``request_journal.append_event``). Each step is "done" once its event has
    landed, carrying the time it landed; nothing is returned beyond what the
    submitter already saw about their own request (the manager's name, the
    decision, how the balance moved, and that the result email was sent).

    Args:
        kind: The stored request kind.
        item_id: The request's item_id.

    Returns:
        ``{"status", "manager_name", "finished", "steps": {...}}``, or None when
        there is no such request (e.g. on the SharePoint path, or once deleted).
    """
    async with async_session() as session:
        request = (await session.execute(
            select(RequestRecord).where(
                RequestRecord.kind == kind, RequestRecord.item_id == item_id
            )
        )).scalars().first()
        if request is None:
            return None                                        # nothing to follow
        events = (await session.execute(
            select(RequestEvent)
            .where(RequestEvent.request_id == request.id)
            .order_by(RequestEvent.id)                         # oldest first, so the first time per type wins
        )).scalars().all()
        decider = await _decided_by_label(session, request)    # who decided, for the decided step (#371)

    first_at: dict[str, str | None] = {}                       # event type -> when it first landed (ISO)
    manager_from_event: str | None = None                      # the manager named on the approval send
    balance_type: str | None = None                            # the balance event that fired, if any
    decided_result: str | None = None                          # "approved" or "rejected"
    for event in events:
        at = event.created_at.isoformat() if event.created_at else None
        first_at.setdefault(event.type, at)                    # keep the earliest time per type
        if event.type == "sent_to_manager" and manager_from_event is None:
            manager_from_event = (event.detail or {}).get("manager")   # the name on the send
        if event.type in _BALANCE_EVENTS and balance_type is None:
            balance_type = event.type                          # the first balance outcome
        if event.type in _DECISION_EVENTS and decided_result is None:
            decided_result = event.type                        # the first decision

    sent_at = first_at.get("sent_to_manager")
    decided_at = first_at.get(decided_result) if decided_result else None
    balance_at = first_at.get(balance_type) if balance_type else None
    result_at = first_at.get("result_sent")

    decided_done = decided_result is not None
    result_sent_done = "result_sent" in first_at
    # Stop following once the decision is made and its email has gone, or the
    # request was cancelled (nothing more will happen to it).
    finished = (decided_done and result_sent_done) or request.status == "cancelled"
    return {
        "status": request.status,                              # lower-case lifecycle state
        "manager_name": manager_from_event or request.manager_name or "",
        "finished": finished,
        "steps": {
            "sent_to_manager": {"done": sent_at is not None, "at": sent_at},
            "decided": {
                "done": decided_done,
                "at": decided_at,
                "result": decided_result,
                "decider": decider if decided_done else None,  # who decided ("an admin" or a name), once decided
            },
            "balance": {
                "done": balance_type is not None,
                "at": balance_at,
                "effect": _BALANCE_EFFECT[balance_type] if balance_type else None,
            },
            "result_sent": {"done": result_sent_done, "at": result_at},
        },
    }
