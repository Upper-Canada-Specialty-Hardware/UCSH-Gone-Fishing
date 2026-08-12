import hashlib
import hmac
import logging
import re
from base64 import b64encode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# A trailing "x22" / "ext. 4" / "#5" / ",,123" is a desk-phone extension, not part
# of the number. It has to be caught on the raw string before the digits are
# joined up, because once punctuation is stripped an extension is indistinguishable
# from a longer number — "+1 416 555 0101 x22" and a real 13-digit number both
# reduce to 13 digits.
_EXTENSION_SUFFIX = re.compile(r"(?:x|ext\.?|#|,)\s*\d+\s*$", re.IGNORECASE)

# E.164 allows at most 15 digits including the country code. The lower bound is
# deliberately loose: national number lengths vary widely outside North America,
# and rejecting a real number costs a manager their texts.
_E164_MIN_DIGITS = 8
_E164_MAX_DIGITS = 15


def normalize_phone(raw: str | None) -> str | None:
    """Convert a Staff Directory phone value into an E.164 number for Twilio.

    The Staff Directory CellNumber column has no input validation, so it holds
    whatever each person typed: bare digits, "(416) 555-1234", a stray trailing
    space, a leading country code. Only the digits carry meaning, so this
    discards every other character and rebuilds the number — the same approach
    the inbound SMS handler already uses to match a texting sender back to their
    Staff Directory record.

    Args:
        raw: The stored phone value, in any format. May be None or blank.

    Returns:
        The number as E.164 (e.g. "+14165551234"), or None when the value
        cannot be resolved to a valid number — too few digits, junk, or a desk
        extension appended to it. Callers treat None as "this person has no
        reachable number" and skip the text.
    """
    if not raw:
        # Blank or None: nothing to dial. Caller skips.
        return None

    if _EXTENSION_SUFFIX.search(raw.strip()):
        # An extension can't be reached by SMS, and the digits before it are a
        # real number belonging to someone — texting it would reach a stranger.
        # Checked ahead of the +/national split so both formats refuse alike.
        return None

    digits = re.sub(r"\D", "", raw)  # keep digits only; drops spaces, dashes, parens

    if raw.strip().startswith("+"):
        # Already international — the country code is in the digits, so don't
        # prepend one. Any plausible E.164 length passes; junk is rejected.
        if _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS:
            return f"+{digits}"
        return None

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # drop a North American trunk prefix, e.g. "14165551234"

    if len(digits) != 10:
        # Not a valid North American number — usually an appended extension.
        # Return None rather than truncating: a truncated number is a real
        # number belonging to someone else, so guessing would text a stranger.
        return None

    return f"+1{digits}"


async def send_sms(to: str, body: str):
    """Send one SMS through the Twilio REST API.

    An unreachable number is skipped rather than raised. This matters because
    callers send inside a loop over an employee's managers: a raise here would
    abort that loop and cost every remaining manager both their text and their
    approval email.

    Args:
        to: Recipient number in any stored format; normalized before sending.
        body: Plain-text message body.

    Returns:
        Twilio's JSON response for the created message, or None when the number
        could not be normalized and nothing was sent.

    Raises:
        httpx.HTTPStatusError: If Twilio rejects a well-formed request, e.g. the
            recipient has replied STOP or the account is suspended.
    """
    number = normalize_phone(to)  # tolerate whatever format SharePoint stored
    if not number:
        # Log loudly: a manager silently missing texts is invisible otherwise.
        logger.warning("Skipping SMS — cannot normalize phone number %r", to)
        return None
    to = number

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
