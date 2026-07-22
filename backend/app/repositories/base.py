"""Repository interfaces — the data-access seam.

Every method returns data in the **SharePoint response shape** the rest of the
app already depends on: a list item is `{"id": <str>, "fields": {<SP column
name>: <value>, ...}}`. The SharePoint implementations pass that straight
through; the Postgres implementations (added in the cutover PRs) will *build*
that same shape from a model row. Keeping the shape identical is what lets the
services and the balance engine's pure functions stay untouched when a domain
is switched from SharePoint to Postgres.
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
    """Company Holidays (stat holidays + half-Friday season markers)."""

    @abstractmethod
    async def get_all(self) -> list[dict]: ...

    @abstractmethod
    async def get_by_id(self, item_id: str | int) -> dict | None: ...


class RequestRepository(ABC):
    """Shared interface for the three request lists (leave / overtime /
    carryover-payout). They differ only in which list they back, so one
    implementation is instantiated once per list."""

    @abstractmethod
    async def get_all(self) -> list[dict]: ...

    @abstractmethod
    async def get_by_id(self, item_id: str | int) -> dict: ...

    @abstractmethod
    async def create(self, fields: dict) -> dict: ...

    @abstractmethod
    async def update_fields(self, item_id: str | int, fields: dict) -> dict: ...


class ManagerAssignmentRepository(ABC):
    """Manager assignments. In SharePoint these live inline on the Staff
    Directory `AllManagers` field; in Postgres they become their own table."""

    @abstractmethod
    async def get_all_assignments(self) -> list[dict]: ...
