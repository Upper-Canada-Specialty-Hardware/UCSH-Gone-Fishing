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
from app.models.request_item import (
    CarryoverPayoutRequestItem,
    LeaveRequestItem,
    OvertimeRequestItem,
)
from app.repositories.postgres.requests import PostgresRequestList

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"


def request_list_tables() -> dict[str, PostgresRequestList]:
    """The Postgres table for each request list, keyed by its SharePoint list id.

    Read at call time, so a test or a settings change sees the current ids.

    Returns:
        {SharePoint list id: PostgresRequestList}.
    """
    return {
        settings.SP_LIST_LEAVE_REQUESTS: PostgresRequestList(LeaveRequestItem),
        settings.SP_LIST_OVERTIME_REQUESTS: PostgresRequestList(OvertimeRequestItem),
        settings.SP_LIST_CARRYOVER_PAYOUT: PostgresRequestList(CarryoverPayoutRequestItem),
    }


def requests_in_postgres() -> bool:
    """True once STORAGE_REQUESTS says the request lists live in Postgres.

    Raises:
        NotImplementedError: For a value other than "sharepoint" or "postgres",
            so a typo fails loudly instead of quietly using SharePoint.
    """
    value = (settings.STORAGE_REQUESTS or SHAREPOINT).strip().lower()
    if value not in (SHAREPOINT, POSTGRES):
        raise NotImplementedError(f"STORAGE_REQUESTS must be 'sharepoint' or 'postgres', not '{value}'")
    return value == POSTGRES


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

    def _backend(self, list_id: str):
        """The object that serves this list: a Postgres table or sp_client.

        Args:
            list_id: The SharePoint list id the caller passed.

        Returns:
            A PostgresRequestList when the list is a request list and requests
            live in Postgres; otherwise sp_client.
        """
        if requests_in_postgres():
            table = request_list_tables().get(list_id)
            if table is not None:
                return table
        return sp_client

    async def get_list_items(self, list_id: str, **kwargs) -> list[dict]:
        """Every item of a list (see sp_client.get_list_items)."""
        return await self._backend(list_id).get_list_items(list_id, **kwargs)

    async def get_list_item(self, list_id: str, item_id) -> dict:
        """One item; raises a 404 HTTPStatusError when missing."""
        return await self._backend(list_id).get_list_item(list_id, item_id)

    async def get_list_item_or_none(self, list_id: str, item_id) -> dict | None:
        """One item, or None when missing."""
        return await self._backend(list_id).get_list_item_or_none(list_id, item_id)

    async def create_list_item(self, list_id: str, fields: dict) -> dict:
        """Add an item; returns it in the Graph list-item shape."""
        return await self._backend(list_id).create_list_item(list_id, fields)

    async def update_list_item_fields(self, list_id: str, item_id, fields: dict) -> dict:
        """Merge fields into an item; returns its fields."""
        return await self._backend(list_id).update_list_item_fields(list_id, item_id, fields)

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove an item for good."""
        await self._backend(list_id).delete_list_item(list_id, item_id)


request_store = RequestStore()
