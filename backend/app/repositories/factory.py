"""Repository factory -- hands each domain the storage-backed implementation
selected by its feature flag.

Holidays can be served from either SharePoint or Postgres: STORAGE_HOLIDAYS
("sharepoint" by default) picks which, and both implementations exist. Employees
are SharePoint-only; STORAGE_EMPLOYEES still gates them so selecting an
unimplemented backend fails loudly rather than silently falling back. Selecting
a backend a domain has no implementation for raises a clear error.
"""
from app.config import settings
from app.repositories.base import (
    EmployeeRepository,
    HolidayRepository,
)
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.repositories.sharepoint.employee import SharePointEmployeeRepository
from app.repositories.sharepoint.holidays import SharePointHolidayRepository

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"


def _unsupported(domain: str, backend: str):
    """Raise a clear error for a storage backend a domain does not implement.

    Args:
        domain: The domain name, for the message.
        backend: The unsupported backend value that was selected.

    Raises:
        NotImplementedError: Always.
    """
    raise NotImplementedError(
        f"Storage backend '{backend}' for {domain} is not implemented. "
        f"'{SHAREPOINT}' is always valid; keep {domain} on '{SHAREPOINT}' "
        f"unless a '{POSTGRES}' implementation exists for it."
    )


def get_employee_repository() -> EmployeeRepository:
    """The Staff Directory repository for the configured backend.

    Returns:
        A SharePoint-backed employee repository (the only implementation).

    Raises:
        NotImplementedError: If STORAGE_EMPLOYEES selects another backend.
    """
    if settings.STORAGE_EMPLOYEES == SHAREPOINT:
        return SharePointEmployeeRepository()
    _unsupported("employees", settings.STORAGE_EMPLOYEES)


def get_holiday_repository() -> HolidayRepository:
    """The Company Holidays repository for the configured backend.

    Returns:
        SharePoint- or Postgres-backed holiday repository per STORAGE_HOLIDAYS.

    Raises:
        NotImplementedError: If STORAGE_HOLIDAYS selects an unknown backend.
    """
    if settings.STORAGE_HOLIDAYS == SHAREPOINT:
        return SharePointHolidayRepository()
    if settings.STORAGE_HOLIDAYS == POSTGRES:
        return PostgresHolidayRepository()
    _unsupported("holidays", settings.STORAGE_HOLIDAYS)
