"""Repository factory -- hands each domain the storage-backed implementation
selected by its feature flag.

Holidays can be served from either SharePoint or Postgres, and the live
``holidays_storage`` app setting (not the ``STORAGE_HOLIDAYS`` env value alone)
decides which, so the storage switch flips them without a redeploy. There is no
fallback: once the setting says Postgres, holidays are served from Postgres and a
miss or error there surfaces rather than silently reading SharePoint. Employees
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
from app.services.app_settings import holidays_storage

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


def _normalise(value: str) -> str:
    """Trim and lower-case a storage backend value for comparison.

    Args:
        value: The raw backend value from the env or a setting, possibly None.

    Returns:
        The value trimmed and lower-cased, defaulting to "sharepoint" when empty.
    """
    return (value or SHAREPOINT).strip().lower()


async def holidays_in_postgres() -> bool:
    """True once Company Holidays live in Postgres, by override or DB setting.

    ``STORAGE_HOLIDAYS`` is the environment override: ``"postgres"`` forces
    Postgres (it lets an operator pin the backend from the environment).
    Otherwise the live ``holidays_storage`` app setting decides, so the storage
    switch flips holidays without a redeploy. There is no fallback to SharePoint
    once either says Postgres.

    Returns:
        True when Company Holidays should be read and written in Postgres.

    Raises:
        NotImplementedError: For an override or setting that is neither
            "sharepoint" nor "postgres", so a typo fails loudly.
    """
    override = _normalise(settings.STORAGE_HOLIDAYS)           # the env pin, if any
    if override not in (SHAREPOINT, POSTGRES):
        _unsupported("holidays", settings.STORAGE_HOLIDAYS)
    if override == POSTGRES:
        return True                                            # the environment pins Postgres
    raw = await holidays_storage()                             # the live DB setting the switch writes
    setting = _normalise(raw)
    if setting not in (SHAREPOINT, POSTGRES):
        _unsupported("holidays", raw)
    return setting == POSTGRES


async def _holiday_backend() -> HolidayRepository:
    """Pick the holiday repository for the live backend, with no fallback.

    Reads ``holidays_in_postgres`` on every call, so the storage switch takes
    effect without a redeploy. Once that says Postgres there is no fallback to
    SharePoint: a miss or error in the Postgres repository surfaces.

    Returns:
        A Postgres- or SharePoint-backed holiday repository.

    Raises:
        NotImplementedError: If the holidays backend is an unknown value.
    """
    if await holidays_in_postgres():
        return PostgresHolidayRepository()
    return SharePointHolidayRepository()


class _HolidayRepositoryProxy(HolidayRepository):
    """A holiday repository that resolves its backend per call.

    ``get_holiday_repository`` stays synchronous so callers keep ``await
    repo.method()`` unchanged, but the backend is a live decision: each method
    reads ``holidays_storage`` (via ``_holiday_backend``) at call time and
    forwards to whichever repository is current. There is no fallback between
    backends; a miss or error from the chosen one surfaces to the caller.
    """

    async def get_all(self) -> list[dict]:
        """Every holiday row from the live backend, in the {"id","fields"} shape."""
        return await (await _holiday_backend()).get_all()

    async def get_by_id(self, item_id: str | int) -> dict | None:
        """One holiday by id from the live backend, or None when missing."""
        return await (await _holiday_backend()).get_by_id(item_id)

    async def create(self, fields: dict) -> dict:
        """Insert a holiday in the live backend; returns {"id","fields"}."""
        return await (await _holiday_backend()).create(fields)

    async def update_fields(self, item_id: str | int, fields: dict) -> dict:
        """Patch a holiday in the live backend; returns {"id","fields"}."""
        return await (await _holiday_backend()).update_fields(item_id, fields)

    async def delete(self, item_id: str | int) -> None:
        """Delete a holiday from the live backend. Raises KeyError if absent."""
        await (await _holiday_backend()).delete(item_id)


# One stateless proxy; each call resolves the backend afresh.
_holiday_repository_proxy = _HolidayRepositoryProxy()


def get_holiday_repository() -> HolidayRepository:
    """The Company Holidays repository seam.

    Returns a proxy that reads the ``holidays_storage`` app setting on every
    operation, so the storage switch flips holidays without a redeploy and
    without any caller change. Once the setting says Postgres there is no
    fallback to SharePoint.

    Returns:
        A holiday repository whose backend is chosen per call.
    """
    return _holiday_repository_proxy
