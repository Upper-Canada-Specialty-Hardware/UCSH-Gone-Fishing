import hashlib
import hmac
import logging
from base64 import b64encode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


async def send_sms(to: str, body: str):
    """Send SMS via Twilio REST API."""
    # Rehearsal mode drops any recipient not on the allow list, so a rehearsal
    # never texts real staff. Gated here, the lowest SMS send, so every caller
    # (approvals, reminders) is covered (#339).
    from app.services.rehearsal import allow_recipient

    if not allow_recipient(to):
        logger.info("Rehearsal: SMS to a non-allowlisted number was dropped")
        return {"skipped": "rehearsal"}

    # Normalize phone number
    if not to.startswith("+"):
        to = f"+1{to[-10:]}"

    url = f"{TWILIO_API_BASE}/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            auth=auth,
            data={
                "From": settings.TWILIO_PHONE_NUMBER,
                "To": to,
                "Body": body,
            },
        )
        resp.raise_for_status()
        logger.info("SMS sent to %s", to)
        return resp.json()


def validate_twilio_signature(url: str, params: dict, signature: str) -> bool:
    """Validate X-Twilio-Signature header."""
    # Build the data string per Twilio's spec
    data = url
    for key in sorted(params.keys()):
        data += key + params[key]

    expected = b64encode(
        hmac.new(
            settings.TWILIO_AUTH_TOKEN.encode(),
            data.encode(),
            hashlib.sha1,
        ).digest()
    ).decode()

    return hmac.compare_digest(expected, signature)
