import logging
import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.services.sms import validate_twilio_signature, send_sms
from app.services.employee import get_employee_by_name, get_employee_by_id, ADMIN_NAMES
from app.services.leave_requests import approve_leave_request, reject_leave_request
from app.services.overtime_requests import approve_overtime_request, reject_overtime_request
from app.services.carryover_payout import approve_carryover_payout, reject_carryover_payout
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)
router = APIRouter()

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

    body = params.get("Body", "").strip()
    # Extract last 10 digits
    from_digits = re.sub(r"\D", "", from_number)[-10:]

    # Parse decision, request ID, and request type from body
    decision, item_id, request_type = _parse_sms_body(body)
    if not decision or not item_id or not request_type:
        await send_sms(
            from_number,
            "Could not understand your response. "
            "Reply 'LR Approve {ID}' or 'LR Reject {ID}' for leave, "
            "'OT Approve {ID}' or 'OT Reject {ID}' for overtime, "
            "'CO Approve {ID}' or 'CO Reject {ID}' for carry over/payout.",
        )
        return ""

    config = REQUEST_TYPE_CONFIG[request_type]

    # Look up the request
    try:
        item = await sp_client.get_list_item(config["list_id"], item_id)
    except Exception:
        await send_sms(from_number, f"Request #{item_id} does not exist, please try again.")
        return ""

    fields = item.get("fields", {})

    # Already processed check
    if config["is_processed"](fields):
        await send_sms(from_number, f"Request #{item_id} has already been processed and archived.")
        return ""

    # Look up SMS sender by cell number (not indexed — client-side filter)
    all_staff = await sp_client.get_list_items(settings.SP_LIST_STAFF_DIRECTORY)
    items = [
        s for s in all_staff
        if re.sub(r"\D", "", s.get("fields", {}).get("CellNumber", ""))[-10:] == from_digits
    ]
    if not items:
        await send_sms(from_number, f"Invalid response - your number is not registered in the system.")
        return ""

    sender = items[0]
    sender_name = sender["fields"].get("Title", "")
    sender_id = sender["id"]

    # Manager authorization check — dynamic lookup from Staff Directory AllManagers
    from app.services.employee import resolve_person_field, get_all_managers_for_employee
    submitter = None
    if config["submitter_field"]:
        submitter = await resolve_person_field(
            fields.get(config["submitter_field"][0]) or fields.get(config["submitter_field"][1])
        )
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

    # Process
    if decision == "Approve":
        result = await config["approve"](item_id, sender_id)
        manager_email = sender["fields"].get("EmailAddress", "")
        await send_sms(
            from_number,
            f"Response has been received. An email will be sent to ({manager_email}) once the process is completed.",
        )
    else:
        result = await config["reject"](item_id, sender_id)
        await send_sms(from_number, f"Response has been received. Cancelling request #{item_id}.")

    return ""


# Prefix → request type mapping
_PREFIX_MAP = {
    "lr": "leave",
    "ot": "overtime",
    "co": "carryover-payout",
}


def _parse_sms_body(body: str) -> tuple[str | None, str | None, str | None]:
    """Extract decision (Approve/Reject), item ID, and request type from SMS body.

    Expected formats:
        LR Approve 123
        OT Reject 45
        CO Approve 12
    """
    body_lower = body.lower().strip()

    # Check for prefix
    request_type = None
    for prefix, rtype in _PREFIX_MAP.items():
        if body_lower.startswith(prefix + " "):
            request_type = rtype
            body_lower = body_lower[len(prefix) + 1:].strip()
            break

    if not request_type:
        return None, None, None

    decision = None
    if "approve" in body_lower:
        decision = "Approve"
    elif "reject" in body_lower or "yeet" in body_lower:
        decision = "Reject"

    # Extract number from remaining body
    numbers = re.findall(r"\d+", body_lower)
    item_id = numbers[0] if numbers else None

    return decision, item_id, request_type
