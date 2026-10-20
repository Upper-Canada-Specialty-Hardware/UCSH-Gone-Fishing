"""Who manages whom: one matching rule for every "is X this person's manager" check (#376).

A person's managers live in their ``AllManagers`` person column: a list of
``{"LookupId": <id>, "LookupValue": <display name>}`` entries. The app used to
decide team membership by comparing ``LookupValue`` with the manager's own staff
name letter for letter. That breaks for a manager set up outside the app (for
example straight in SharePoint), whose Microsoft 365 display name can differ from
their Staff Directory name by case, spacing or spelling: they showed on the
Manager assignments screen but got no My team.

This module matches a manager two ways, and either is enough:

- by id: the entry's ``LookupId`` is one of the manager's own link ids. Before
  the switch to Postgres that is their Microsoft 365 user id (their own
  ``TitleLink`` column, the same id space as ``AllManagers``); after the switch
  every link holds staff ids, so it is their staff id;
- by name, ignoring case and extra spaces.

Everything here is pure (no I/O); the one async helper only asks which storage
is live.
"""

from app.repositories.staff_store import staff_in_postgres


class ManagerName(str):
    """A manager's display name that also carries their link ids.

    It behaves exactly like the plain name string everywhere it is shown or
    compared, so existing callers keep working; ``lists_manager`` and
    ``team_names`` also read ``link_ids`` to match by id.
    """

    link_ids: frozenset[int]

    def __new__(cls, name: str, link_ids: frozenset[int] = frozenset()):
        """Build the name with its ids.

        Args:
            name: The manager's Staff Directory name (Title).
            link_ids: The ids an ``AllManagers`` entry may use for them.

        Returns:
            The name, as a ``str`` with ``link_ids`` attached.
        """
        obj = super().__new__(cls, name)
        obj.link_ids = frozenset(link_ids)                       # immutable: shared safely
        return obj


def normalise_name(name: object) -> str:
    """A name reduced for comparison: lower case, single spaces, trimmed.

    Args:
        name: Any value; None and non-strings give "".

    Returns:
        The comparison form ("" for a blank name, which never matches anyone).
    """
    return " ".join(str(name or "").split()).casefold()       # collapse runs of spaces, fold case


def _as_int(value: object) -> int | None:
    """An id as an int, or None when it is blank or not a number."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def link_ids_for(manager_item: dict, postgres: bool) -> frozenset[int]:
    """The ids an ``AllManagers`` entry may hold for this manager.

    Args:
        manager_item: The manager's ``{"id", "fields"}`` staff item.
        postgres: Whether staff live in Postgres (after the switch).

    Returns:
        Their own ``TitleLinkLookupId`` when set (their Microsoft 365 id before
        the switch, their staff number after it), plus their staff id after the
        switch, when every link is keyed by staff id. Empty when none is known.
    """
    fields = manager_item.get("fields", {}) or {}
    ids = set()
    own_link = _as_int(fields.get("TitleLinkLookupId"))         # their own account link
    if own_link is not None:
        ids.add(own_link)
    if postgres:                                                 # links hold staff ids after the switch
        staff_id = _as_int(manager_item.get("id"))
        if staff_id is not None:
            ids.add(staff_id)
    return frozenset(ids)


async def manager_name_of(manager_item: dict | None) -> ManagerName:
    """The manager's name with their link ids, ready for every team check.

    Args:
        manager_item: The manager's staff item, or None when unknown.

    Returns:
        A ``ManagerName``; an empty one (matches nobody) when unknown.
    """
    if not manager_item:
        return ManagerName("")
    name = (manager_item.get("fields", {}) or {}).get("Title", "") or ""
    return ManagerName(name, link_ids_for(manager_item, await staff_in_postgres()))


def entry_keys(entry: object) -> list:
    """The keys one ``AllManagers`` entry files a report under in a team map.

    Args:
        entry: One ``{"LookupId", "LookupValue"}`` entry.

    Returns:
        The normalised name and ``("id", n)`` for its id, whichever are present.
    """
    if not isinstance(entry, dict):
        return []
    keys: list = []
    name = normalise_name(entry.get("LookupValue"))
    if name:
        keys.append(name)                                        # by name, case and spacing ignored
    lookup = _as_int(entry.get("LookupId"))
    if lookup is not None:
        keys.append(("id", lookup))                              # by id
    return keys


def manager_keys(manager_name: str) -> list:
    """The keys a manager's reports are filed under (see ``entry_keys``).

    Args:
        manager_name: A plain name or a ``ManagerName`` carrying ids.

    Returns:
        The normalised name plus one ``("id", n)`` per link id.
    """
    keys: list = []
    name = normalise_name(manager_name)
    if name:
        keys.append(name)
    keys.extend(("id", i) for i in sorted(getattr(manager_name, "link_ids", ())))
    return keys


def lists_manager(fields: dict, manager_name: str) -> bool:
    """Whether this person's ``AllManagers`` names the manager, by id or by name.

    Args:
        fields: The person's Staff Directory fields.
        manager_name: A plain name or a ``ManagerName`` carrying ids.

    Returns:
        True when any entry matches; False for no managers or a blank manager.
    """
    entries = fields.get("AllManagers")
    if not entries or not isinstance(entries, list):             # nobody manages them
        return False
    wanted = set(manager_keys(manager_name))
    if not wanted:                                               # a blank manager matches nobody
        return False
    return any(key in wanted for entry in entries for key in entry_keys(entry))


def team_names(mgr_to_emp_names: dict, manager_name: str) -> set:
    """The reports filed under any of the manager's keys in a team map.

    Args:
        mgr_to_emp_names: Built with ``entry_keys`` (key -> set of report names).
        manager_name: A plain name or a ``ManagerName`` carrying ids.

    Returns:
        The union of the matching sets (a new set; the map is not changed).
    """
    found: set = set()
    for key in manager_keys(manager_name):
        found |= mgr_to_emp_names.get(key, set())
    return found
