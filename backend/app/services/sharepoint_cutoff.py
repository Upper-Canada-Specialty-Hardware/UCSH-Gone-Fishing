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
import re
from urllib.parse import urlsplit

from app.config import settings

logger = logging.getLogger(__name__)

# The staff-history import (#448) is the one job allowed to read SharePoint after
# the switch. It needs exactly two read-only GETs: the Staff Directory item
# versions it backfills, and the site-id lookup that versions URL is built from
# (the app never resolves the site after the switch, so the id is otherwise
# unknown). These two patterns gate that exception; nothing else, and never a
# write, so the cut-off still refuses every other call. Both are matched against
# the URL path alone, with any query string stripped first, so a query string
# cannot smuggle a path the gate would never allow on its own.
_SITE_LOOKUP = f"/sites/{settings.SP_SITE_HOST}:{settings.SP_SITE_PATH}"   # the site-id resolve GET
_STAFF_VERSIONS = re.compile(                                             # /lists/<staff>/items/<id>/versions
    rf"/lists/{re.escape(settings.SP_LIST_STAFF_DIRECTORY)}/items/[^/]+/versions$"
)


def is_staff_history_read(method: str, path: str) -> bool:
    """Whether a call is a read the staff-history import may make after the switch.

    Args:
        method: The HTTP method of the call.
        path: The Graph path (relative, or an absolute ``@odata.nextLink``).

    Returns:
        True only for a GET of the Staff Directory item versions, or the site-id
        lookup that read depends on. Every write, and every read of another list,
        returns False so the cut-off still refuses it. The path is compared with
        any query string dropped, so a smuggled ``?.../versions`` cannot slip past.
    """
    if method.upper() != "GET":                            # a write is never allowed, flag or not
        return False
    only_path = urlsplit(path).path                        # the path alone; a query string is dropped
    if only_path == _SITE_LOOKUP:                          # the site-id resolve the versions URL needs
        return True
    # End-anchored, so an absolute @odata.nextLink (host + /v1.0/...) still matches
    # on its tail, but the path must genuinely end at .../versions.
    return bool(_STAFF_VERSIONS.search(only_path))         # the Staff Directory versions read itself


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
