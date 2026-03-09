import logging
import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.services.sms import validate_twilio_signature, send_sms
from app.services.employee import get_employee_by_name, ADMIN_NAMES
from app.services.leave_requests import approve_leave_request, reject_leave_request
from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sms", response_class=PlainTextResponse)
async def receive_sms(request: Request):
    """Inbound SMS webhook from Twilio."""
    form = await request.form()
    params = {k: v for k, v in form.items()}

    # Validate Twilio signature
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if not validate_twilio_signature(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    body = params.get("Body", "").strip()
    from_number = params.get("From", "")
    # Extract last 10 digits
    from_digits = re.sub(r"\D", "", from_number)[-10:]

    # Parse decision and request ID from body
    decision, item_id = _parse_sms_body(body)
    if not decision or not item_id:
        await send_sms(from_number, "Could not understand your response. Reply 'Approve {ID}' or 'Reject {ID}'.")
        return ""

    # Look up the leave request
    try:
        item = await sp_client.get_list_item(settings.SP_LIST_LEAVE_REQUESTS, item_id)
    except Exception:
        await send_sms(from_number, f"Request #{item_id} does not exist, please try again.")
        return ""

    fields = item.get("fields", {})

    # Already processed check
    if fields.get("ApproveProcessedFlag") == "Processed" or fields.get("Status") != "Pending":
        await send_sms(from_number, f"Request #{item_id} has already been processed and archived.")
        return ""

    # Look up SMS sender by cell number
    items = await sp_client.get_list_items(
        settings.SP_LIST_STAFF_DIRECTORY,
        filter=f"fields/CellNumber eq '{from_digits}'",
        top=1,
    )
    if not items:
        await send_sms(from_number, f"Invalid response - your number is not registered in the system.")
        return ""

    sender = items[0]
    sender_name = sender["fields"].get("Title", "")
    sender_id = sender["id"]

    # Manager authorization check
    manager_name = fields.get("Managertxt", "")
    if sender_name != manager_name and sender_name not in ADMIN_NAMES:
        await send_sms(from_number, f"Invalid response - you do not have access to request #{item_id}.")
        return ""

    # Process
    if decision == "Approve":
        result = await approve_leave_request(item_id, sender_id)
        manager_email = sender["fields"].get("EmailAddress", "")
        await send_sms(
            from_number,
            f"Response has been received. An email will be sent to ({manager_email}) once the process is completed.",
        )
    else:
        result = await reject_leave_request(item_id, sender_id)
        await send_sms(from_number, f"Response has been received. Cancelling request #{item_id}.")

    return ""


def _parse_sms_body(body: str) -> tuple[str | None, str | None]:
    """Extract decision (Approve/Reject) and item ID from SMS body."""
    body_lower = body.lower()

    decision = None
    if "approve" in body_lower:
        decision = "Approve"
    elif "reject" in body_lower or "yeet" in body_lower:
        decision = "Reject"

    # Extract number from body
    numbers = re.findall(r"\d+", body)
    item_id = numbers[0] if numbers else None

    return decision, item_id
