"""After the storage switch the app never talks to SharePoint again.

Once the switch to Postgres has run, Postgres is the only source of truth for
requests, staff and holidays. Nothing in the app may read or write a SharePoint
list from then on: no fallback to SharePoint when Postgres misses or errors, and
no SharePoint read with Postgres as the fallback. Every Graph call goes through
``GraphClient._request``, which asks ``refuse_if_cut_off`` first, so a code path
that still reaches for SharePoint after the switch fails loudly with
``SharePointCutOff`` instead of quietly using the frozen lists.

The one exception is a write that began before the switch and is still finishing
(it holds the SharePoint pin from ``guarded_write``); the switch waits for those
before it flips, so in practice the exception only covers the switch's own final
steps.
"""
import logging

logger = logging.getLogger(__name__)


class SharePointCutOff(RuntimeError):
    """A SharePoint call was attempted after the switch to Postgres.

    Raised instead of making the call, so a missed call site surfaces as an
    error in the logs (and a 500 for an API caller) rather than reading or
    writing the frozen SharePoint lists.
    """


async def sharepoint_cut_off() -> bool:
    """Whether the switch has run, so SharePoint is off limits.

    The switch writes the requests, staff and holidays settings in one
    transaction, so the requests setting alone tells whether it has run.

    Returns:
        True once requests live in Postgres (the switch has run), unless the
        current operation is a write pinned to SharePoint that began before it.
    """
    from app.repositories.request_store import requests_in_postgres   # lazy: avoid an import cycle
    from app.services.switch_pause import pinned_requests_backend     # lazy: avoid an import cycle
    if pinned_requests_backend() is False:                 # a pre-switch write still finishing
        return False
    return await requests_in_postgres()


async def refuse_if_cut_off(method: str, path: str) -> None:
    """Raise before any Graph call once the switch has run.

    Args:
        method: The HTTP method of the refused call, for the log line.
        path: The Graph path of the refused call, for the log line.

    Raises:
        SharePointCutOff: After the switch to Postgres.
    """
    if await sharepoint_cut_off():
        logger.error("SharePoint call refused after the switch to Postgres: %s %s", method, path)
        raise SharePointCutOff(f"SharePoint is no longer used after the switch ({method} {path})")
