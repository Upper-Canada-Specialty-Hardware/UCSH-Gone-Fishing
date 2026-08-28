"""UTC clock helpers, split by whether the caller needs a tz-aware value.

`datetime.utcnow()` is deprecated and scheduled for removal, but it cannot be
swapped for `datetime.now(timezone.utc)` everywhere, because the two return
different things: the old call returned a **naive** datetime whose fields happen
to be UTC, the new one returns an **aware** datetime carrying `tzinfo`. Mixing
them up breaks two places in this codebase:

* **Naive columns.** `change_token`, `processing_log`, `request_approval_state`,
  `webhook_subscription` and `carryover_reset_log` are plain `DateTime`
  (`timestamp without time zone`). asyncpg rejects an aware value written into
  one, so those must keep receiving naive values.
* **Graph subscription expiry.** `graph/webhooks.py` builds the timestamp as
  `expiration.isoformat() + "Z"`. A naive value renders `...T12:00:00` and the
  appended `Z` correctly marks it UTC; an aware value would render
  `...T12:00:00+00:00` and the same append would produce the malformed
  `...+00:00Z`, which Graph rejects.

So both helpers exist and the name says which you are getting. Pick `_aware` for
`DateTime(timezone=True)` columns, `_naive` for everything listed above.
"""
from datetime import datetime, timezone


def utcnow_aware() -> datetime:
    """Current UTC time, carrying tzinfo.

    Returns:
        A tz-aware datetime. Use with `DateTime(timezone=True)` columns, where
        it round-trips as `timestamptz` on Postgres.
    """
    return datetime.now(timezone.utc)


def utcnow_naive() -> datetime:
    """Current UTC time with no tzinfo — the exact value `utcnow()` returned.

    Drops the tzinfo after converting, so the wall-clock fields are UTC rather
    than local. This is a behaviour-preserving replacement: every naive column
    and serialization keeps storing precisely what it stored before.

    Returns:
        A naive datetime whose fields are UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
