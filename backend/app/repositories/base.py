"""Repository interfaces -- the data-access seam.

Every method returns data in the **SharePoint response shape** the rest of the
app already depends on: a list item is `{"id": <str>, "fields": {<SP column
name>: <value>, ...}}`. The SharePoint implementations pass that straight
through; the Postgres implementation *builds* that same shape from a model row.
Keeping the shape identical is what lets the services and the balance engine's
pure functions stay untouched when a domain is switched from SharePoint to
Postgres.
"""
from abc import ABC, abstractmethod


class EmployeeRepository(ABC):
    """Staff Directory (employees + balances)."""

    @abstractmethod
    async def get_all(self) -> list[dict]: ...

    @abstractmethod
    async def get_by_id(self, item_id: str | int) -> dict | None: ...

    @abstractmethod
    async def get_by_name(self, name: str) -> dict | None: ...

    @abstractmethod
    async def get_by_email(self, email: str) -> dict | None: ...

    @abstractmethod
    async def update_fields(self, item_id: str | int, fields: dict) -> dict: ...


class HolidayRepository(ABC):
    """Company Holidays (stat holidays + half-Friday season markers).

    Writes exist for the admin dashboard's holiday editor -- the app itself never
    writes holidays during request processing.
    """

    @abstractmethod
    async def get_all(self) -> list[dict]: ...

    @abstractmethod
    async def get_by_id(self, item_id: str | int) -> dict | None: ...

    @abstractmethod
    async def create(self, fields: dict) -> dict:
        """Insert a holiday; returns it in the {"id","fields"} shape."""
        ...

    @abstractmethod
    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Patch a holiday's fields; returns the updated {"id","fields"} shape."""
        ...

    @abstractmethod
    async def delete(self, item_id: str | int) -> None:
        """Remove a holiday permanently. Raises KeyError if it does not exist."""
        ...
