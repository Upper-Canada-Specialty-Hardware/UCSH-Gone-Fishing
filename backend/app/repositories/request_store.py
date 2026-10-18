"""Where the three request lists are read and written: SharePoint or Postgres.

``request_store`` has the same list methods as ``sp_client``. Every read and
write of the leave, overtime and carry-over/payout lists goes through it, with
the SharePoint list id it always passed. STORAGE_REQUESTS decides where those
calls land:

- "sharepoint" (the default): straight through to ``sp_client``, as before.
- "postgres": to the matching Postgres table, which behaves like the list
  (see app/repositories/postgres/requests.py).

Any other list id (the Staff Directory, the holidays) always goes to
SharePoint, so a call site that passes a list id from a variable is safe
either way.
"""
from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.postgres.typed_requests import TypedRequestList
from app.services.app_settings import requests_storage
from app.services.switch_pause import pinned_requests_backend  # a guarded write pins the backend

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"


def request_list_tables() -> dict[str, TypedRequestList]:
    """The Postgres table for each request list, keyed by its SharePoint list id.

    Read at call time, so a test or a settings change sees the current ids. Each
    list is served by the typed ``requests`` table (one ``RequestRecord`` row per
    request, told apart by kind), so the live request services read and write
    real columns while still seeing the SharePoint list-item shapes (#321). The
    pre-move JSON copy path keeps its own ``PostgresRequestList`` tables inside
    request_copy.py; this is the live store.

    Returns:
        {SharePoint list id: TypedRequestList}.
    """
    return {
        settings.SP_LIST_LEAVE_REQUESTS: TypedRequestList("leave"),
        settings.SP_LIST_OVERTIME_REQUESTS: TypedRequestList("overtime"),
        settings.SP_LIST_CARRYOVER_PAYOUT: TypedRequestList("carryover_payout"),
    }


def _validate(label: str, value: str) -> str:
    """Normalise a storage value and fail loudly on a typo.

    Args:
        label: The setting's name, for the error message.
        value: The raw value.

    Returns:
        The trimmed, lower-cased value.

    Raises:
        NotImplementedError: For anything but "sharepoint" or "postgres".
    """
    value = (value or SHAREPOINT).strip().lower()
    if value not in (SHAREPOINT, POSTGRES):
        raise NotImplementedError(f"{label} must be 'sharepoint' or 'postgres', not '{value}'")
    return value


async def requests_in_postgres() -> bool:
    """True once the request lists live in Postgres, by override or DB setting.

    The config ``STORAGE_REQUESTS`` is an override: ``"postgres"`` forces Postgres
    (tests set it this way, and it lets an operator pin the backend from the
    environment). Otherwise the live ``requests_storage`` app setting decides, so
    an admin can flip the switch without a redeploy.

    Returns:
        True when requests should be read and written in Postgres.

    Raises:
        NotImplementedError: For an override or setting that is neither
            "sharepoint" nor "postgres", so a typo fails loudly.
    """
    override = _validate("STORAGE_REQUESTS", settings.STORAGE_REQUESTS)
    if override == POSTGRES:
        return True                                             # the environment pins Postgres
    return _validate("requests_storage", await requests_storage()) == POSTGRES


def is_request_list(list_id: str) -> bool:
    """Whether a list id is one of the three request lists.

    Args:
        list_id: A SharePoint list id.

    Returns:
        True for the leave, overtime and carry-over/payout lists.
    """
    return list_id in request_list_tables()


class RequestStore:
    """sp_client's list methods, routed per STORAGE_REQUESTS for the request lists."""

    async def _backend(self, list_id: str):
        """The object that serves this list: a Postgres table or sp_client.

        Async because the switch now reads the live ``requests_storage`` setting
        from the database (``requests_in_postgres``), not just a config value.

        Args:
            list_id: The SharePoint list id the caller passed.

        Returns:
            A request-list table when the list is a request list and requests
            live in Postgres; otherwise sp_client.
        """
        # A guarded write pins the backend for its whole run, so a decision that
        # started on SharePoint still reads and writes SharePoint even if the
        # switch flips the setting underneath it; with no guard, read the setting.
        pinned = pinned_requests_backend()
        in_postgres = pinned if pinned is not None else await requests_in_postgres()
        if in_postgres:
            table = request_list_tables().get(list_id)
            if table is not None:
                return table
        return sp_client

    async def get_list_items(self, list_id: str, **kwargs) -> list[dict]:
        """Every item of a list (see sp_client.get_list_items)."""
        return await (await self._backend(list_id)).get_list_items(list_id, **kwargs)

    async def get_list_item(self, list_id: str, item_id) -> dict:
        """One item; raises a 404 HTTPStatusError when missing."""
        return await (await self._backend(list_id)).get_list_item(list_id, item_id)

    async def get_list_item_or_none(self, list_id: str, item_id) -> dict | None:
        """One item, or None when missing."""
        return await (await self._backend(list_id)).get_list_item_or_none(list_id, item_id)

    async def create_list_item(self, list_id: str, fields: dict) -> dict:
        """Add an item; returns it in the Graph list-item shape."""
        return await (await self._backend(list_id)).create_list_item(list_id, fields)

    async def update_list_item_fields(self, list_id: str, item_id, fields: dict) -> dict:
        """Merge fields into an item; returns its fields."""
        return await (await self._backend(list_id)).update_list_item_fields(list_id, item_id, fields)

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove an item for good."""
        await (await self._backend(list_id)).delete_list_item(list_id, item_id)


request_store = RequestStore()
