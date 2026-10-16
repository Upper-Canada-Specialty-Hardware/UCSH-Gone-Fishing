"""Rehearsal mode: exercise the Postgres switch without touching the outside (#339).

A rehearsal run proves the import, the shadow sync and the flip work end to end
on real data, but must not act on the world: no Graph webhook subscriptions get
registered, nothing is written back to SharePoint (reads stay, because the
importer needs them), and emails and texts go only to a short allow list of the
people running the rehearsal. Everything else is logged and dropped.

Three questions are asked from the call sites that could reach the outside, so
the gate lives in one place:

- ``rehearsal_enabled()`` from the webhook registration and the SharePoint write
  methods.
- ``allow_recipient(address_or_phone)`` from the outbox sender and from today's
  direct email and SMS send functions, which drop anyone not on the list.
- ``sharepoint_writes_allowed()`` from ``sp_client``'s create, update and delete.

Its own site address and admin password are the ones already configured
(``DASHBOARD_FRONTEND_URL``, ``BASE_URL`` and the admin password stored in
Postgres), so a rehearsal needs no new address or password settings. Only the
two ``REHEARSAL_*`` settings in ``config.py`` turn it on and name the allow list.
"""

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def rehearsal_enabled() -> bool:
    """Whether the app is in rehearsal mode.

    Returns:
        True when ``REHEARSAL_MODE`` is set, so the outside-world gates engage.
    """
    return bool(settings.REHEARSAL_MODE)                        # one master switch


def _normalise(value: str) -> str:
    """Reduce one email or phone number to a comparable key.

    An email is compared lower case and trimmed; a phone number is compared as
    its last ten digits, so ``+1 (647) 555-0100`` and ``6475550100`` match.

    Args:
        value: An email address or phone number.

    Returns:
        The comparison key, or "" when there is nothing to compare.
    """
    text = (value or "").strip()                                # drop surrounding space
    if not text:
        return ""
    if "@" in text:
        return text.lower()                                     # an email: case-insensitive
    digits = "".join(ch for ch in text if ch.isdigit())         # a phone: digits only
    return digits[-10:] if digits else ""                       # last ten, so country code does not matter


def _allowlist() -> set[str]:
    """The allowed recipients, each reduced to its comparison key.

    Returns:
        The set of normalised entries from ``REHEARSAL_ALLOWLIST`` (comma
        separated emails and phone numbers).
    """
    raw = settings.REHEARSAL_ALLOWLIST or ""                    # comma-separated, may be blank
    return {key for key in (_normalise(entry) for entry in raw.split(",")) if key}


def allow_recipient(address_or_phone: str) -> bool:
    """Whether a message may be sent to one recipient.

    Args:
        address_or_phone: The email address or phone number a send is aimed at.

    Returns:
        True outside rehearsal; in rehearsal, True only when the recipient is on
        ``REHEARSAL_ALLOWLIST``. A blank recipient is never allowed in rehearsal.
    """
    if not rehearsal_enabled():
        return True                                             # normal operation: send to anyone
    key = _normalise(address_or_phone)                          # reduce to the comparable form
    return bool(key) and key in _allowlist()                    # only the rehearsal team


def sharepoint_writes_allowed() -> bool:
    """Whether writing back to SharePoint is permitted right now.

    Returns:
        False in rehearsal, so ``sp_client``'s write methods log and skip while
        its reads keep working for the importer.
    """
    return not rehearsal_enabled()                              # reads always, writes never in rehearsal
