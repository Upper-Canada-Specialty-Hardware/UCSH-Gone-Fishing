import hashlib
import hmac
import logging
import re
from base64 import b64encode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# A Twilio Account SID: "AC" + 32 hex. Matched so it can be scrubbed from stored
# error text, which carries the API URL (and so the SID) (#420).
_ACCOUNT_SID_RE = re.compile(r"AC[0-9a-fA-F]{32}")

# A phone-like run: an optional "+", a digit, then digits and the usual
# separators (spaces, dots, dashes, brackets), ending on a digit. It may match
# more than a phone number (a long id, a date), so a run is masked only once it
# carries at least seven digits (finding 5); over-masking error text is safe,
# leaking a real number is not.
_PHONE_LIKE_RE = re.compile(r"\+?\d[\d\s().\-]{5,}\d")
_MIN_PHONE_DIGITS = 7


class InvalidPhoneNumber(ValueError):
    """A phone number that cannot be turned into an E.164 number (#417).

    Raised by ``normalize_phone`` so the outbox can treat a bad recipient as a
    permanent failure (no point retrying a number Twilio will always refuse)
    rather than a transient one.
    """


def normalize_phone(to: str) -> str:
    """Turn a stored phone number into E.164 (``+1XXXXXXXXXX``), or raise.

    The old rule took the last ten CHARACTERS, so "(416) 949-9131" became
    "+1) 949-9131" and Twilio refused it. This reduces to DIGITS first, so
    punctuation and spaces never reach the number (#417).

    Args:
        to: The number as stored (any formatting), or already "+"-prefixed.

    Returns:
        The number in E.164: a given "+" keeps its "+" and digits; ten digits
        become ``+1`` plus those digits; eleven digits starting with 1 become
        ``+`` plus those digits.

    Raises:
        InvalidPhoneNumber: The value has no usable digits or is not a shape
            that maps to a North American E.164 number.
    """
    raw = (to or "").strip()                                       # tolerate None and stray space
    digits = re.sub(r"\D", "", raw)                                # keep only the digits
    if raw.startswith("+"):                                        # already E.164: keep "+" and its digits
        if not digits:
            # Mask the number in the message: the exception text is stored and
            # logged, so the raw number must never ride along in it (#420).
            raise InvalidPhoneNumber(f"no digits in phone number {mask_phone(to)}")
        return "+" + digits
    if len(digits) == 10:                                          # local 10-digit number: add +1
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):              # 1 + area code + number
        return "+" + digits
    raise InvalidPhoneNumber(f"cannot turn {mask_phone(to)} into an E.164 phone number")


def mask_phone(value: str) -> str:
    """Hide all but the last four digits of a phone number, for logs and display (#420).

    Args:
        value: A phone number in any format, or None.

    Returns:
        ``***1234`` keeping the last four digits, or ``****`` when there are
        four or fewer digits (nothing safe to show).
    """
    digits = re.sub(r"\D", "", value or "")                        # compare on digits only
    if len(digits) <= 4:
        return "****"                                              # too short to reveal anything
    return "***" + digits[-4:]                                     # keep only the last four


def scrub_twilio_sid(text: str | None) -> str | None:
    """Remove the Twilio Account SID from text about to be stored or logged (#420).

    The httpx error text includes the request URL, which carries the Account
    SID; a stored error must not leak it. Both the configured SID and any
    ``AC`` + 32 hex token are replaced.

    Args:
        text: The error text, or None.

    Returns:
        The text with every Account SID replaced by ``AC****``; None stays None.
    """
    if not text:
        return text                                                # nothing to scrub
    sid = settings.TWILIO_ACCOUNT_SID
    if sid:
        text = text.replace(sid, "AC****")                         # the configured SID, verbatim
    return _ACCOUNT_SID_RE.sub("AC****", text)                     # and any SID-shaped token in a URL


def _mask_phone_run(match: re.Match) -> str:
    """Mask one phone-like run, or leave it alone when it is too short to be one.

    Args:
        match: A ``_PHONE_LIKE_RE`` match over some error text.

    Returns:
        ``***1234`` (last four digits) when the run holds at least seven digits,
        else the run unchanged (a short number is nothing worth hiding).
    """
    chunk = match.group(0)
    digits = re.sub(r"\D", "", chunk)                              # the run's digits only
    return mask_phone(chunk) if len(digits) >= _MIN_PHONE_DIGITS else chunk


def mask_phone_numbers(text: str | None) -> str | None:
    """Mask every phone-like number in text about to be stored or logged (#420).

    Twilio's own 4xx messages quote the recipient verbatim (e.g. "The 'To'
    number +1416... is not a valid phone number."), so a stored error can leak a
    real number unless it is masked first.

    Args:
        text: The error text, or None.

    Returns:
        The text with each phone-like run reduced to its last four digits; None
        stays None.
    """
    if not text:
        return text                                                # nothing to mask
    return _PHONE_LIKE_RE.sub(_mask_phone_run, text)               # mask each phone-like run in place


def scrub_error_text(text: str | None) -> str | None:
    """Scrub both the Twilio Account SID and any phone number from error text (#420).

    The one pass every send error goes through before it is stored, logged, or
    written to a timeline event, so neither the account SID (in the API URL) nor
    the recipient's number ever leaks.

    Args:
        text: The error text, or None.

    Returns:
        The text with the SID and every phone-like run masked; None stays None.
    """
    return mask_phone_numbers(scrub_twilio_sid(text))              # SID first, then numbers


async def send_sms(to: str, body: str):
    """Send SMS via Twilio REST API."""
    # Rehearsal mode drops any recipient not on the allow list, so a rehearsal
    # never texts real staff. Gated here, the lowest SMS send, so every caller
    # (approvals, reminders) is covered (#339).
    from app.services.rehearsal import allow_recipient

    if not allow_recipient(to):
        logger.info("Rehearsal: SMS to %s was dropped (not on the allowlist)", mask_phone(to))
        return {"skipped": "rehearsal"}

    # Normalize to E.164 on digits, not the last ten characters, so a formatted
    # number reaches Twilio correctly; a number it cannot parse raises (#417).
    to = normalize_phone(to)

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
        logger.info("SMS sent to %s", mask_phone(to))              # never log the full number (#420)
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
