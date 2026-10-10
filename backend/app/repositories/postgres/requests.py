"""The three request lists served from Postgres, behaving like SharePoint.

``PostgresRequestList`` answers the same calls the SharePoint client answers for
a list (``get_list_items``, ``get_list_item``, ``get_list_item_or_none``,
``create_list_item``, ``update_list_item_fields``, ``delete_list_item``) and
returns the same shapes Graph returns:

- an item is ``{"id": "<id>", "createdDateTime", "lastModifiedDateTime",
  "fields": {...}}``, with ``id`` as a string the way Graph sends it;
- ``fields`` carries every stored column value plus the ``id``, ``Created`` and
  ``Modified`` values SharePoint adds to every item;
- an update merges the given fields into the item (a PATCH), stamps
  ``Modified``, and returns the item's fields;
- a missing item raises the same ``httpx.HTTPStatusError`` (404) Graph raises,
  so every caller's existing error handling keeps working.

The request services, approval flow, reminders and dashboards therefore need no
change beyond asking ``app.repositories.request_store`` instead of
``sp_client``.
"""
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, text

from app.database import async_session
from app.models.mixins import utcnow
from app.models.request_item import RequestItemMixin

logger = logging.getLogger(__name__)

# Values SharePoint fills in on every item; the app never writes them itself.
SYSTEM_FIELDS = ("id", "Created", "Modified")


def _iso(moment: datetime | None) -> str | None:
    """Format a timestamp the way Graph does ("2026-10-09T14:03:22Z").

    Args:
        moment: An aware (or naive UTC) datetime, or None.

    Returns:
        The UTC timestamp to the second with a trailing Z, or None.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:                                   # SQLite hands back naive values
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _not_found(list_id: str, item_id) -> httpx.HTTPStatusError:
    """The error Graph raises for a missing item, so callers handle it the same way.

    Args:
        list_id: The SharePoint list id the caller asked about.
        item_id: The missing item id.

    Returns:
        An httpx.HTTPStatusError carrying a 404 response.
    """
    request = httpx.Request("GET", f"postgres://requests/{list_id}/items/{item_id}")   # for the error message only
    response = httpx.Response(404, request=request)
    return httpx.HTTPStatusError(f"Item {item_id} not found", request=request, response=response)


def _to_int(item_id) -> int | None:
    """An item id as an int, or None when it cannot be one (so it is not found).

    Args:
        item_id: The id as the caller passed it (str or int).

    Returns:
        The int id, or None.
    """
    try:
        return int(item_id)
    except (TypeError, ValueError):
        return None


def item_shape(row: RequestItemMixin) -> dict:
    """Build the Graph list-item shape from a row.

    Args:
        row: A request table row.

    Returns:
        {"id", "createdDateTime", "lastModifiedDateTime", "fields"}; fields
        includes SharePoint's own id, Created and Modified values.
    """
    created = _iso(row.created_at)
    modified = _iso(row.modified_at)
    fields = dict(row.fields or {})                             # a copy: callers may mutate it
    fields.update({"id": str(row.id), "Created": created, "Modified": modified})
    return {
        "id": str(row.id),
        "createdDateTime": created,
        "lastModifiedDateTime": modified,
        "fields": fields,
    }


def _stored(fields: dict) -> dict:
    """The fields worth storing: everything but the values SharePoint computes.

    Args:
        fields: Fields as a caller passed them.

    Returns:
        A new dict without id, Created and Modified.
    """
    return {k: v for k, v in (fields or {}).items() if k not in SYSTEM_FIELDS}


class PostgresRequestList:
    """One request list (leave, overtime or carry-over/payout) in Postgres.

    Args:
        model: The table's model class (LeaveRequestItem and so on).
    """

    def __init__(self, model: type[RequestItemMixin]):
        self.model = model

    async def get_list_items(
        self,
        list_id: str,
        filter: str | None = None,
        select: list[str] | None = None,
        expand: str = "fields",
        top: int = 5000,
    ) -> list[dict]:
        """Every item in the list, oldest first, like an unfiltered Graph read.

        Args:
            list_id: The SharePoint list id (kept for the same call shape).
            filter: Not supported; no request-list read uses an OData filter.
            select: When given, only these fields are returned (plus id).
            expand: Ignored; fields are always included, as with "fields".
            top: Ignored; Graph pages through everything, and so does this.

        Returns:
            The items in the Graph list-item shape.

        Raises:
            NotImplementedError: If an OData filter is passed.
        """
        if filter:
            raise NotImplementedError("OData filters are not supported on the Postgres request lists")
        async with async_session() as session:
            rows = (await session.execute(select_rows(self.model))).scalars().all()
        items = [item_shape(row) for row in rows]
        if select:                                              # $select keeps only the named columns
            keep = set(select) | {"id"}
            for item in items:
                item["fields"] = {k: v for k, v in item["fields"].items() if k in keep}
        return items

    async def get_list_item(self, list_id: str, item_id) -> dict:
        """One item, or the 404 error Graph raises when it does not exist.

        Args:
            list_id: The SharePoint list id.
            item_id: The item id (str or int).

        Returns:
            The item in the Graph list-item shape.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        key = _to_int(item_id)
        async with async_session() as session:
            row = await session.get(self.model, key) if key is not None else None
        if row is None:
            raise _not_found(list_id, item_id)
        return item_shape(row)

    async def get_list_item_or_none(self, list_id: str, item_id) -> dict | None:
        """get_list_item, but a missing item is None instead of an error.

        Args:
            list_id: The SharePoint list id.
            item_id: The item id.

        Returns:
            The item, or None.
        """
        try:
            return await self.get_list_item(list_id, item_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def create_list_item(self, list_id: str, fields: dict, *, item_id: int | None = None,
                               sp_item_id: str | None = None, created_at: datetime | None = None,
                               modified_at: datetime | None = None) -> dict:
        """Add an item, as Graph's POST .../items does.

        Args:
            list_id: The SharePoint list id.
            fields: The new item's column values.
            item_id: Keep this id (copying from SharePoint); None takes the next id.
            sp_item_id: The SharePoint item this row came from, if any.
            created_at: The original creation time (copying); None means now.
            modified_at: The original modification time (copying); None means now.

        Returns:
            The new item in the Graph list-item shape.
        """
        now = utcnow()
        row = self.model(
            fields=_stored(fields),
            created_at=created_at or now,
            modified_at=modified_at or created_at or now,
            sp_item_id=sp_item_id,
        )
        if item_id is not None:
            row.id = item_id                                    # a copied item keeps its SharePoint id
        async with async_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return item_shape(row)

    async def update_list_item_fields(self, list_id: str, item_id, fields: dict) -> dict:
        """Merge fields into an item, as Graph's PATCH .../fields does.

        Fields not named are left as they are; a field set to None is cleared.

        Args:
            list_id: The SharePoint list id.
            item_id: The item id.
            fields: The column values to change.

        Returns:
            The item's fields after the change (what Graph's PATCH returns).

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        key = _to_int(item_id)
        async with async_session() as session:
            row = await session.get(self.model, key) if key is not None else None
            if row is None:
                raise _not_found(list_id, item_id)
            merged = dict(row.fields or {})
            merged.update(_stored(fields))
            row.fields = merged                                 # a new dict, so the JSON change is saved
            row.modified_at = utcnow()
            await session.commit()
            await session.refresh(row)
        return item_shape(row)["fields"]

    async def delete_list_item(self, list_id: str, item_id) -> None:
        """Remove an item for good, as Graph's DELETE does.

        Args:
            list_id: The SharePoint list id.
            item_id: The item id.

        Raises:
            httpx.HTTPStatusError: 404 when there is no such item.
        """
        key = _to_int(item_id)
        async with async_session() as session:
            row = await session.get(self.model, key) if key is not None else None
            if row is None:
                raise _not_found(list_id, item_id)
            await session.delete(row)
            await session.commit()

    async def save_copy(self, item_id: int, fields: dict, created_at: datetime | None,
                        modified_at: datetime | None) -> bool:
        """Write a SharePoint item into this table under its own id (insert or overwrite).

        Used only by the copy from SharePoint, before requests move: the
        SharePoint item is still the original, so its fields replace whatever
        an earlier copy stored.

        Args:
            item_id: The SharePoint item id, kept as this row's id.
            fields: The item's fields as Graph returned them.
            created_at: The item's createdDateTime.
            modified_at: The item's lastModifiedDateTime.

        Returns:
            True when the row was new, False when an earlier copy was overwritten.
        """
        async with async_session() as session:
            row = await session.get(self.model, item_id)
            created = row is None
            if created:
                row = self.model(id=item_id)
                session.add(row)
            row.fields = _stored(fields)
            row.created_at = created_at or utcnow()
            row.modified_at = modified_at or row.created_at
            row.sp_item_id = str(item_id)
            await session.commit()
        return created

    async def count(self) -> int:
        """How many items the table holds.

        Returns:
            The row count.
        """
        async with async_session() as session:
            return len((await session.execute(select(self.model.id))).all())

    async def sp_item_ids(self) -> set[str]:
        """The SharePoint item ids already copied or moved into this table.

        Returns:
            A set of SharePoint item ids, as strings.
        """
        async with async_session() as session:
            rows = (await session.execute(
                select(self.model.sp_item_id).where(self.model.sp_item_id.is_not(None))
            )).all()
        return {r[0] for r in rows}

    async def find_by_sp_item_id(self, sp_item_id: str) -> dict | None:
        """The row copied or moved from a given SharePoint item, if there is one.

        Args:
            sp_item_id: The SharePoint item id.

        Returns:
            The item in the Graph list-item shape, or None.
        """
        async with async_session() as session:
            row = (await session.execute(
                select(self.model).where(self.model.sp_item_id == str(sp_item_id))
            )).scalars().first()
        return item_shape(row) if row else None

    async def sync_id_sequence(self) -> None:
        """Move Postgres's id counter past the highest id, after ids were set by hand.

        Copied items keep their SharePoint ids, which the id counter does not
        see; without this the next new request could be given an id already in
        use. SQLite needs nothing: it always continues from the highest id.
        """
        async with async_session() as session:
            if session.bind.dialect.name != "postgresql":
                return
            table = self.model.__tablename__
            await session.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            ))
            await session.commit()


def select_rows(model):
    """Every row of a request table, oldest id first.

    Args:
        model: The table's model class.

    Returns:
        A SQLAlchemy select statement.
    """
    return select(model).order_by(model.id)
