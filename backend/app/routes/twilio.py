import logging
import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.services.sms import validate_twilio_signature, send_sms, scrub_error_text
from app.services.sms_api_log import (
    OUTCOME_ALREADY_DECIDED,
    OUTCOME_APPROVED,
    OUTCOME_BAD_SIGNATURE,
    OUTCOME_ERROR,
    OUTCOME_NO_ACCESS,
    OUTCOME_NOT_FOUND,
    OUTCOME_NOT_REGISTERED,
    OUTCOME_PAUSED,
    OUTCOME_PROCESSING_OFF,
    OUTCOME_REFUSED,
    OUTCOME_REJECTED,
    OUTCOME_UNRECOGNIZED,
    apply_delivery_update,
    record_reply,
    update_reply,
)
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
    """Inbound SMS webhook from Twilio.

    The decision logic is unchanged (same replies, same status codes, same order
    of checks); the only addition is the Text Log (#494). One ``sms_reply_log``
    row is written per inbound text, created up front so the instant reply we
    send back can be tied to it, and its ``outcome`` plus the parsed fields are
    filled in at the end whatever branch the handler took. Logging never raises,
    so it cannot change what the sender receives.
    """
    logger.info("Inbound SMS webhook received from %s", request.client.host if request.client else "unknown")
    form = await request.form()
    params = {k: v for k, v in form.items()}

    # Read the inbound fields once, for the webhook logic and for the reply log.
    from_number = params.get("From", "")
    to_number = params.get("To", "")
    inbound_sid = params.get("MessageSid", "")
    inbound_body = params.get("Body", "")

    # Validate Twilio signature — use BASE_URL (not request.url) to match
    # the public URL that Twilio signed against, since Railway's proxy
    # rewrites the internal URL scheme/host.
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.BASE_URL}/api/twilio/sms"
    signature_ok = validate_twilio_signature(url, params, signature)

    # Record the inbound up front so every instant reply we send can link back to
    # it (#494). Returns None if the write failed; the handler then just sends no
    # link and behaves exactly as before.
    reply_id = await record_reply(
        message_sid=inbound_sid, from_number=from_number, to_number=to_number,
        body=inbound_body, signature_ok=signature_ok,
    )
    # What update_reply is told at the end; mutated as the flow decides the outcome.
    verdict = {
        "outcome": OUTCOME_ERROR, "decision": None, "request_type": None,
        "item_id": None, "reason": None, "sender_staff_id": None, "error_text": None,
    }

    async def reply(text: str) -> None:
        """Send one instant reply, labelled ``reply`` and tied to this inbound (#494).

        Args:
            text: The message to text back to the sender.
        """
        await send_sms(from_number, text, kind="reply", inbound_reply_id=reply_id)

    try:
        if not signature_ok:
            verdict["outcome"] = OUTCOME_BAD_SIGNATURE            # forged/misconfigured inbound
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        # Gate behind processing toggle
        if not settings.PROCESSING_ENABLED:
            await reply("System is currently in reporting-only mode. Approvals via SMS are disabled.")
            verdict["outcome"] = OUTCOME_PROCESSING_OFF
            return ""

        # While the storage switch runs, reply with the same friendly sentence the API
        # routes return, so an SMS approval is refused cleanly rather than half-applied.
        if await is_paused():
            await reply(PAUSE_MESSAGE)
            verdict["outcome"] = OUTCOME_PAUSED
            return ""

        body = params.get("Body", "").strip()
        # Extract last 10 digits
        from_digits = re.sub(r"\D", "", from_number)[-10:]

        # Parse decision, request ID, request type, and (for a reject) the reason.
        decision, item_id, request_type, reason = _parse_sms_body(body)
        verdict.update(decision=decision, request_type=request_type, item_id=item_id, reason=reason)
        if not decision or not item_id or not request_type:
            await reply(UNRECOGNIZED_SMS_REPLY)
            verdict["outcome"] = OUTCOME_UNRECOGNIZED
            return ""

        config = REQUEST_TYPE_CONFIG[request_type]

        # Look up the request
        try:
            item = await request_store.get_list_item(config["list_id"], item_id)
        except Exception:
            await reply(f"Request #{item_id} does not exist, please try again.")
            verdict["outcome"] = OUTCOME_NOT_FOUND
            return ""

        fields = item.get("fields", {})

        # Already processed check
        if config["is_processed"](fields):
            await reply(f"Request #{item_id} has already been processed and archived.")
            verdict["outcome"] = OUTCOME_ALREADY_DECIDED
            return ""

        # Look up SMS sender by cell number (not indexed — client-side filter)
        all_staff = await staff_store.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
        items = [
            s for s in all_staff
            if re.sub(r"\D", "", s.get("fields", {}).get("CellNumber", ""))[-10:] == from_digits
        ]
        if not items:
            await reply("Invalid response - your number is not registered in the system.")
            verdict["outcome"] = OUTCOME_NOT_REGISTERED
            return ""

        sender = items[0]
        sender_name = sender["fields"].get("Title", "")
        sender_id = sender["id"]
        verdict["sender_staff_id"] = sender_id                    # the matched staff member

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
            await reply(f"Invalid response - you do not have access to request #{item_id}.")
            verdict["outcome"] = OUTCOME_NO_ACCESS
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
                await reply(f"Request #{item_id} was not approved. {result['error']}")
                verdict["outcome"] = OUTCOME_REFUSED
                verdict["error_text"] = result["error"]
                return ""
            manager_email = sender["fields"].get("EmailAddress", "")
            await reply(
                f"Response has been received. An email will be sent to ({manager_email}) once the process is completed.",
            )
            verdict["outcome"] = OUTCOME_APPROVED
        else:
            # channel="sms"; no link version by SMS. Any words after the number in
            # the reply become the reason (#433); no words means no reason, as before.
            result = await config["reject"](item_id, sender_id, "sms", None, reason) or {}
            if result.get("error"):
                await reply(f"Request #{item_id} was not rejected. {result['error']}")
                verdict["outcome"] = OUTCOME_REFUSED
                verdict["error_text"] = result["error"]
                return ""
            await reply(f"Response has been received. Cancelling request #{item_id}.")
            verdict["outcome"] = OUTCOME_REJECTED

        return ""
    except HTTPException:
        raise                                                     # bad signature: outcome already set
    except Exception as e:                                        # unexpected: record and re-raise unchanged
        verdict["outcome"] = OUTCOME_ERROR
        verdict["error_text"] = scrub_error_text(f"{type(e).__name__}: {e}")
        raise
    finally:
        # Fill in the inbound row whatever path ran; never raises.
        await update_reply(
            reply_id,
            outcome=verdict["outcome"], decision=verdict["decision"],
            request_type=verdict["request_type"], item_id=verdict["item_id"],
            reason=verdict["reason"], sender_staff_id=verdict["sender_staff_id"],
            error_text=verdict["error_text"],
        )


@router.post("/status", response_class=PlainTextResponse)
async def twilio_status_callback(request: Request):
    """Delivery status callback from Twilio for a message we sent (#496).

    Twilio POSTs here as a message moves through queued -> sent -> delivered (or
    undelivered / failed), because every ``send_sms`` sets ``StatusCallback`` to
    this url. The signature is validated against this exact url the same way the
    inbound webhook validates its own (403 on a bad one). The matching send row
    is then updated with the latest status and the step is appended to its
    history. A status callback carries no business data, so it is recorded even
    when processing is off or the storage switch is paused. An unknown SID is a
    no-op, and a DB problem is swallowed, so Twilio always gets a 200 and does not
    retry needlessly.
    """
    form = await request.form()
    params = {k: v for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{settings.BASE_URL}/api/twilio/status"
    if not validate_twilio_signature(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    # MessageStatus is the new status; ErrorCode is set on undelivered/failed.
    await apply_delivery_update(
        params.get("MessageSid", ""), params.get("MessageStatus"), params.get("ErrorCode"),
    )
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
