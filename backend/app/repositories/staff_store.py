"""Where the Staff Directory list is read and written: SharePoint or Postgres.

``staff_store`` has the same list methods as ``sp_client``. Every read and write
of the Staff Directory list that goes through it lands wherever ``staff_storage``
says, with the SharePoint list id it always passed:

- "sharepoint" (the default): straight through to ``sp_client``, as before.
- "postgres": to the ``staff`` table, which answers in the SharePoint item shape
  (see app/repositories/postgres/staff.py).

Any other list id (the request lists, the holidays) always goes to SharePoint,
so a call site that passes a list id from a variable is safe either way. This
mirrors ``app/repositories/request_store.py`` so the two switches behave the
same way for whoever reads them.
"""
from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.postgres.staff import PostgresStaffList
from app.services.app_settings import staff_storage

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"

# One shared Postgres adapter; it holds no per-call state.
_postgres_staff = PostgresStaffList()


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


async def staff_in_postgres() -> bool:
    """True once the Staff Directory lives in Postgres, by override or DB setting.

    ``STORAGE_EMPLOYEES`` is the environment override: ``"postgres"`` forces
    Postgres (tests set it this way, and it lets an operator pin the backend from
    the environment). Otherwise the live ``staff_storage`` app setting decides,
    so an admin can flip the switch without a redeploy.

    Returns:
        True when the Staff Directory should be read and written in Postgres.

    Raises:
        NotImplementedError: For an override or setting that is neither
            "sharepoint" nor "postgres", so a typo fails loudly.
    """
    override = _validate("STORAGE_EMPLOYEES", settings.STORAGE_EMPLOYEES)
    if override == POSTGRES:
        return True                                             # the environment pins Postgres
    return _validate("staff_storage", await staff_storage()) == POSTGRES


def is_staff_list(list_id: str) -> bool:
    """Whether a list id is the Staff Directory list.

    Args:
        list_id: A SharePoint list id.

    Returns:
        True only for the Staff Directory list.
    """
    return list_id == settings.SP_LIST_STAFF_DIRECTORY


class StaffStore:
    """sp_client's list methods, routed per ``staff_storage`` for the Staff Directory."""

    async def _backend(self, list_id: str):
        """The object that serves this list: the Postgres staff table or sp_client.

        Async because the switch reads the live ``staff_storage`` setting from the
        database (``staff_in_postgres``), not just a config value.

        Args:
            list_id: The SharePoint list id the caller passed.

        Returns:
            The Postgres staff adapter when the list is the Staff Directory and
            staff live in Postgres; otherwise sp_client.
        """
        if is_staff_list(list_id) and await staff_in_postgres():
            return _postgres_staff
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
        """Add an item; returns it in the SharePoint item shape."""
        return await (await self._backend(list_id)).create_list_item(list_id, fields)

    async def update_list_item_fields(
        self, list_id: str, item_id, fields: dict, *,
        links: dict | None = None, history_source: str | None = None,
        history_reason: str | None = None,
    ) -> dict:
        """Merge fields into an item; returns its fields.

        ``links``, ``history_source`` and ``history_reason`` only apply to a staff
        write served from Postgres: they carry the manager/supervisor names the
        Graph write shape lacks and ask the store to record the change in
        ``staff_changes`` once. SharePoint reads the ``*LookupId`` keys already in
        ``fields`` and records no history, so it is passed only the plain payload
        and its behaviour is unchanged.

        Args:
            list_id: The SharePoint list id the caller passed.
            item_id: The item id to merge into.
            fields: The SharePoint column values to change.
            links: Manager/supervisor links with names, for the Postgres backend.
            history_source: ``"system"`` / ``"admin_dashboard"`` to record history in
                Postgres, or None when the caller records its own.
            history_reason: The reason stored with that history.

        Returns:
            The item's fields after the change.
        """
        backend = await self._backend(list_id)
        if backend is _postgres_staff:                    # the Postgres staff table takes the extras
            return await backend.update_list_item_fields(
                list_id, item_id, fields,
                links=links, history_source=history_source, history_reason=history_reason,
            )
        return await backend.update_list_item_fields(list_id, item_id, fields)

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove an item for good."""
        await (await self._backend(list_id)).delete_list_item(list_id, item_id)


staff_store = StaffStore()
