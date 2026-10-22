import logging
import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.services.sms import validate_twilio_signature, send_sms
from app.services.employee import get_employee_by_id, ADMIN_NAMES
from app.services.leave_requests import approve_leave_request, reject_leave_request
from app.services.overtime_requests import approve_overtime_request, reject_overtime_request
from app.services.carryover_payout import approve_carryover_payout, reject_carryover_payout
from app.repositories.request_store import request_store
from app.repositories.staff_store import staff_store  # Staff Directory reads/writes follow staff_storage
from app.services.switch_pause import PAUSE_MESSAGE, is_paused
from app.services.request_decisions import MAX_REJECT_REASON_LEN  # the reject-reason length cap (finding 14)

logger = logging.getLogger(__name__)
router = APIRouter()

# The reply sent when an inbound SMS cannot be parsed. It shows that a reject may
# carry a free-text reason after the number, and that the reason is optional, so
# a bare "LR Reject 123" still works (owner ruling) (finding 2).
UNRECOGNIZED_SMS_REPLY = (
    "Could not understand your response. "
    "Reply 'LR Approve {ID}' or 'LR Reject {ID} <reason>' for leave, "
    "'OT Approve {ID}' or 'OT Reject {ID} <reason>' for overtime, "
    "'CO Approve {ID}' or 'CO Reject {ID} <reason>' for carry over/payout. "
    "A reject reason is optional."
)

REQUEST_TYPE_CONFIG = {
    "leave": {
        "list_id": settings.SP_LIST_LEAVE_REQUESTS,
        "is_processed": lambda f: f.get("ApproveProcessedFlag") == "Processed" or f.get("Status") != "Pending",
        "submitter_field": ("SubmittedTest", "SubmittedTestLookupId"),
        "approve": approve_leave_request,
        "reject": reject_leave_request,
    },
    "overtime": {
        "list_id": settings.SP_LIST_OVERTIME_REQUESTS,
        "is_processed": lambda f: f.get("Status") != "Pending",
        "submitter_field": ("SubmittedBy", "SubmittedByLookupId"),
        "approve": approve_overtime_request,
        "reject": reject_overtime_request,
    },
    "carryover-payout": {
        "list_id": settings.SP_LIST_CARRYOVER_PAYOUT,
        "is_processed": lambda f: f.get("SystemState") == "Processed",
        "submitter_field": None,  # uses EmployeeID directly
        "approve": approve_carryover_payout,
        "reject": reject_carryover_payout,
    },
}


@router.post("/sms", response_class=PlainTextResponse)
async def receive_sms(request: Request):
    """Inbound SMS webhook from Twilio."""
    logger.info("Inbound SMS webhook received from %s", request.client.host if request.client else "unknown")
    form = await request.form()
    params = {k: v for k, v in form.items()}

    # Validate Twilio signature — use BASE_URL (not request.url) to match
    # the public URL that Twilio signed against, since Railway's proxy
    # rewrites the internal URL scheme/host.
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.BASE_URL}/api/twilio/sms"
    if not validate_twilio_signature(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    from_number = params.get("From", "")

    # Gate behind processing toggle
    if not settings.PROCESSING_ENABLED:
        await send_sms(from_number, "System is currently in reporting-only mode. Approvals via SMS are disabled.")
        return ""

    # While the storage switch runs, reply with the same friendly sentence the API
    # routes return, so an SMS approval is refused cleanly rather than half-applied.
    if await is_paused():
        await send_sms(from_number, PAUSE_MESSAGE)
        return ""

    body = params.get("Body", "").strip()
    # Extract last 10 digits
    from_digits = re.sub(r"\D", "", from_number)[-10:]

    # Parse decision, request ID, request type, and (for a reject) the reason.
    decision, item_id, request_type, reason = _parse_sms_body(body)
    if not decision or not item_id or not request_type:
        await send_sms(from_number, UNRECOGNIZED_SMS_REPLY)
        return ""

    config = REQUEST_TYPE_CONFIG[request_type]

    # Look up the request
    try:
        item = await request_store.get_list_item(config["list_id"], item_id)
    except Exception:
        await send_sms(from_number, f"Request #{item_id} does not exist, please try again.")
        return ""

    fields = item.get("fields", {})

    # Already processed check
    if config["is_processed"](fields):
        await send_sms(from_number, f"Request #{item_id} has already been processed and archived.")
        return ""

    # Look up SMS sender by cell number (not indexed — client-side filter)
    all_staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    items = [
        s for s in all_staff
        if re.sub(r"\D", "", s.get("fields", {}).get("CellNumber", ""))[-10:] == from_digits
    ]
    if not items:
        await send_sms(from_number, "Invalid response - your number is not registered in the system.")
        return ""

    sender = items[0]
    sender_name = sender["fields"].get("Title", "")
    sender_id = sender["id"]

    # Manager authorization check — dynamic lookup from Staff Directory AllManagers
    from app.services.employee import get_all_managers_for_employee
    from app.services.request_submitter import resolve_request_submitter
    submitter = None
    if config["submitter_field"]:
        # SubmitterEmail first, then the person column named in the config.
        submitter = await resolve_request_submitter(fields, config["submitter_field"][0])
    else:
        # Carryover/payout uses EmployeeID directly
        employee_id = fields.get("EmployeeID")
        if employee_id:
            submitter = await get_employee_by_id(employee_id)

    authorized = sender_name in ADMIN_NAMES
    if not authorized and submitter:
        mgrs = await get_all_managers_for_employee(submitter)
        for mgr in mgrs:
            if mgr["fields"].get("Title", "") == sender_name:
                authorized = True
                break
    if not authorized:
        await send_sms(from_number, f"Invalid response - you do not have access to request #{item_id}.")
        return ""

    # Process. The handlers report a refusal in their result rather than raising
    # — a clash with an approved absence, or an action another channel already
    # took — so read it before telling the manager their reply went through.
    # Replying "response has been received" regardless made a refused approval
    # indistinguishable from a successful one.
    if decision == "Approve":
        # channel="sms"; SMS replies carry no link version, so stale-link
        # checking does not apply here (link_version stays the default None).
        result = await config["approve"](item_id, sender_id, "sms") or {}
        if result.get("error"):
            await send_sms(from_number, f"Request #{item_id} was not approved. {result['error']}")
            return ""
        manager_email = sender["fields"].get("EmailAddress", "")
        await send_sms(
            from_number,
            f"Response has been received. An email will be sent to ({manager_email}) once the process is completed.",
        )
    else:
        # channel="sms"; no link version by SMS. Any words after the number in
        # the reply become the reason (#433); no words means no reason, as before.
        result = await config["reject"](item_id, sender_id, "sms", None, reason) or {}
        if result.get("error"):
            await send_sms(from_number, f"Request #{item_id} was not rejected. {result['error']}")
            return ""
        await send_sms(from_number, f"Response has been received. Cancelling request #{item_id}.")

    return ""


# Prefix → request type mapping
_PREFIX_MAP = {
    "lr": "leave",
    "ot": "overtime",
    "co": "carryover-payout",
}


def _parse_sms_body(body: str) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract decision, item ID, request type, and a reject reason from an SMS.

    Expected formats (the reason is anything after the number, reject only):
        LR Approve 123
        OT Reject 45
        CO Reject 12 covered already

    The item number splits the command from the reason: the DECISION word is read
    only from the text BEFORE the number, and the reason only from AFTER it.
    Reading the decision from the whole message let a reject reason that mentioned
    "approve" ("LR Reject 123 can't approve, short staffed") flip the decision to
    Approve (finding 1). The decision is matched case-insensitively; the reason
    keeps the sender's original wording.

    Args:
        body: The raw SMS body.

    Returns:
        ``(decision, item_id, request_type, reason)``; each is None when absent.
        The reason is None for approvals and for a reject with no trailing words,
        and is capped at ``MAX_REJECT_REASON_LEN`` characters (finding 14).
    """
    stripped = body.strip()                                    # keep original case for the reason
    body_lower = stripped.lower()                              # match the prefix/decision case-insensitively

    # Check for a prefix, and keep the remainder in its original case.
    request_type = None
    remainder = stripped                                       # the text after the prefix, original case
    for prefix, rtype in _PREFIX_MAP.items():
        if body_lower.startswith(prefix + " "):
            request_type = rtype
            remainder = stripped[len(prefix) + 1:].strip()     # drop "LR "/"OT "/"CO "
            break

    if not request_type:
        return None, None, None, None

    # The first run of digits is the item id; the command is before it, the reason
    # after it. With no number, the whole remainder is the command (no item id).
    match = re.search(r"\d+", remainder)
    item_id = match.group(0) if match else None
    before = remainder[: match.start()] if match else remainder   # the command, before the number
    after = remainder[match.end():].strip() if match else ""      # the reason, after the number

    before_lower = before.lower()                              # decision matched on the command only
    decision = None
    # Reject first: "disapprove" contains "approve", so an approve-first test would
    # misread it; a reject word always wins over an approve substring in it.
    if "reject" in before_lower or "disapprove" in before_lower or "yeet" in before_lower:
        decision = "Reject"
    elif "approve" in before_lower:
        decision = "Approve"

    reason = after or None                                     # no trailing words means no reason
    if reason and len(reason) > MAX_REJECT_REASON_LEN:         # cap the reason at this entry (finding 14)
        reason = reason[:MAX_REJECT_REASON_LEN]

    return decision, item_id, request_type, reason
