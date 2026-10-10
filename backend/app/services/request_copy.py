"""Copy the three request lists from SharePoint into Postgres, before the move (#194).

``copy_requests_from_sharepoint`` reads every item of the leave, overtime and
carry-over/payout lists and writes each into its Postgres table under the same
id, so approval links, SMS replies and approval state that name an item keep
working after STORAGE_REQUESTS is set to "postgres". It can run any number of
times while SharePoint is still the original: each run overwrites the earlier
copy with SharePoint's current values.

Once requests live in Postgres the copy refuses to run, because SharePoint's
older values would then overwrite newer decisions made in Postgres.

Each copied item also gets SubmitterEmail when it has none, looked up from its
person column, so every request can be traced to its employee by email alone.
"""
import logging
from datetime import datetime

from app.config import settings
from app.graph.sharepoint import sp_client
from app.repositories.request_store import request_list_tables, requests_in_postgres
from app.services.request_submitter import SUBMITTER_EMAIL_COLUMN

logger = logging.getLogger(__name__)

# The person column naming each list's submitter, by setting name of its list id.
PERSON_COLUMNS = {
    "SP_LIST_LEAVE_REQUESTS": "SubmittedTest",
    "SP_LIST_OVERTIME_REQUESTS": "SubmittedBy",
    "SP_LIST_CARRYOVER_PAYOUT": "SubmittedBy",
}

# A readable name for each list in the report.
LIST_NAMES = {
    "SP_LIST_LEAVE_REQUESTS": "leave",
    "SP_LIST_OVERTIME_REQUESTS": "overtime",
    "SP_LIST_CARRYOVER_PAYOUT": "carryover_payout",
}


class RequestsAlreadyMoved(Exception):
    """The copy was asked for after requests moved to Postgres."""


def _when(value) -> datetime | None:
    """Parse a Graph timestamp ("2026-10-09T14:03:22Z").

    Args:
        value: The raw value, or None.

    Returns:
        An aware datetime, or None when missing or unreadable.
    """
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _lookup_id(fields: dict, column: str) -> int | None:
    """The SharePoint user id in a person column, in either shape Graph uses.

    Args:
        fields: The item's fields.
        column: The person column, e.g. "SubmittedTest".

    Returns:
        The user id, or None.
    """
    raw = fields.get(f"{column}LookupId")
    if raw is None and isinstance(fields.get(column), dict):
        raw = fields[column].get("LookupId")                   # the expanded person shape
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def _user_emails() -> dict[int, str]:
    """SharePoint user id -> email, from the site's User Information List.

    Returns:
        The map; empty if the list cannot be read (emails are then left unfilled).
    """
    try:
        users = await sp_client.get_list_items("User Information List", top=5000)
    except Exception:
        logger.exception("Request copy: could not read the User Information List")
        return {}
    emails = {}
    for user in users:
        email = (user.get("fields", {}).get("EMail") or "").strip().lower()
        if email:
            emails[int(user["id"])] = email
    return emails


def copy_fields(fields: dict, person_column: str, emails: dict[int, str]) -> dict:
    """The fields to store for one SharePoint item.

    Drops Graph's own @odata values and fills SubmitterEmail from the person
    column when the item has no email yet.

    Args:
        fields: The item's fields as Graph returned them.
        person_column: This list's submitter person column.
        emails: SharePoint user id -> email.

    Returns:
        A new dict of fields.
    """
    stored = {k: v for k, v in fields.items() if not k.startswith("@odata")}
    if not stored.get(SUBMITTER_EMAIL_COLUMN):
        email = emails.get(_lookup_id(fields, person_column))
        if email:
            stored[SUBMITTER_EMAIL_COLUMN] = email
    return stored


async def copy_requests_from_sharepoint() -> dict:
    """Copy every request from the three SharePoint lists into Postgres.

    Returns:
        {"lists": {name: {"source_count", "created", "updated"}}}.

    Raises:
        RequestsAlreadyMoved: When STORAGE_REQUESTS is already "postgres".
    """
    if requests_in_postgres():
        raise RequestsAlreadyMoved(
            "Requests already live in Postgres; copying from SharePoint now would overwrite newer data."
        )
    emails = await _user_emails()
    tables = request_list_tables()
    report: dict = {}
    for setting_name, name in LIST_NAMES.items():
        list_id = getattr(settings, setting_name)
        table = tables[list_id]
        items = await sp_client.get_list_items(list_id)
        created = updated = 0
        for item in items:
            fields = copy_fields(item.get("fields", {}), PERSON_COLUMNS[setting_name], emails)
            was_new = await table.save_copy(
                int(item["id"]), fields,
                _when(item.get("createdDateTime")), _when(item.get("lastModifiedDateTime")),
            )
            created += was_new
            updated += not was_new
        await table.sync_id_sequence()                          # new requests continue after the highest copied id
        report[name] = {"source_count": len(items), "created": created, "updated": updated}
        logger.info("Request copy (%s): %d read, %d created, %d updated", name, len(items), created, updated)
    return {"lists": report}


async def request_storage_report() -> dict:
    """Where requests live now, and how the two sides compare. Reads only.

    Returns:
        {"storage": "sharepoint" | "postgres", "lists": {name:
        {"sharepoint_count", "postgres_count", "only_in_sharepoint"}}}, where
        only_in_sharepoint counts SharePoint items with no Postgres row yet.
        A list SharePoint cannot be read for carries "error" instead.
    """
    tables = request_list_tables()
    lists: dict = {}
    for setting_name, name in LIST_NAMES.items():
        list_id = getattr(settings, setting_name)
        table = tables[list_id]
        entry: dict = {"postgres_count": await table.count()}
        try:
            items = await sp_client.get_list_items(list_id, select=["Title"])   # ids only, kept small
            copied = await table.sp_item_ids()
            entry["sharepoint_count"] = len(items)
            entry["only_in_sharepoint"] = sum(1 for item in items if str(item["id"]) not in copied)
        except Exception as e:
            logger.exception("Request storage report: could not read %s", name)
            entry["error"] = str(e)
        lists[name] = entry
    return {"storage": "postgres" if requests_in_postgres() else "sharepoint", "lists": lists}
