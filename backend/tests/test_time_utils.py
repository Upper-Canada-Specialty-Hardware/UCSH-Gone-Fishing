"""Pins the naive/aware split that replaced datetime.utcnow().

The swap is only safe because the two helpers are NOT interchangeable. These
tests fail if someone later "simplifies" them into one, which is the mistake the
split exists to prevent.
"""
from datetime import datetime, timedelta, timezone

from app.time_utils import utcnow_aware, utcnow_naive


def test_naive_helper_has_no_tzinfo():
    """Naive columns reject an aware value, so this must stay naive.

    change_token, processing_log, request_approval_state, webhook_subscription
    and carryover_reset_log are plain DateTime (timestamp without time zone).
    asyncpg raises on an aware value written into one.
    """
    assert utcnow_naive().tzinfo is None


def test_aware_helper_carries_utc():
    """DateTime(timezone=True) columns need the offset to round-trip."""
    now = utcnow_aware()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_naive_helper_reads_utc_not_local_time():
    """The fields must be UTC, not the machine's wall clock.

    Dropping tzinfo from an aware UTC value keeps UTC fields. Calling
    datetime.now() and dropping tzinfo would silently store local time, which on
    a Toronto dev box is 4-5 hours off and would skew every reminder and
    approval-version timestamp.
    """
    delta = abs(utcnow_naive() - datetime.now(timezone.utc).replace(tzinfo=None))
    assert delta < timedelta(seconds=5)


def test_the_two_helpers_agree_on_the_instant():
    """Same moment, different representation - not different clocks."""
    naive = utcnow_naive()
    aware = utcnow_aware()
    assert abs(aware.replace(tzinfo=None) - naive) < timedelta(seconds=5)


def test_graph_subscription_expiry_serializes_without_a_double_offset():
    """Regression pin for graph/webhooks.py `expiration.isoformat() + "Z"`.

    Naive renders `...T12:00:00`, so the appended Z correctly marks it UTC. An
    aware value would render `...T12:00:00+00:00` and the same append produces
    `...+00:00Z`, which Microsoft Graph rejects - the subscription silently
    stops renewing and SharePoint webhooks go dead 29 days later.
    """
    serialized = (utcnow_naive() + timedelta(days=29)).isoformat() + "Z"

    assert serialized.endswith("Z")
    assert "+00:00" not in serialized
    # Round-trips as a real UTC timestamp rather than merely looking like one.
    parsed = datetime.fromisoformat(serialized.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)

    # And demonstrate the trap rather than just avoiding it: the aware helper
    # through the same expression produces the malformed value. This is what
    # fails if someone later collapses the two helpers into one.
    wrong = (utcnow_aware() + timedelta(days=29)).isoformat() + "Z"
    assert wrong.endswith("+00:00Z")
