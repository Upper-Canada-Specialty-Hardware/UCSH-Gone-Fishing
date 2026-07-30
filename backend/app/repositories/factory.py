"""Repository factory — hands each domain the storage-backed implementation
selected by its feature flag.

Every flag still defaults to "sharepoint", so the factory returns the
SharePoint implementations unless a flag is explicitly set (no behavior change
on deploy). Each cutover PR adds a Postgres implementation for one domain and
wires its "postgres" branch here; the flag is flipped separately, once that
domain's rows have been backfilled. Selecting a backend a domain has no
implementation for raises a clear error rather than silently falling back to
SharePoint.

Implemented so far: holidays and employees (sharepoint | postgres). Requests
remain SharePoint-only.
"""
from app.config import settings
from app.repositories.base import (
    EmployeeRepository,
    HolidayRepository,
    ManagerAssignmentRepository,
    RequestRepository,
)
from app.repositories.sharepoint.employee import SharePointEmployeeRepository
from app.repositories.sharepoint.holidays import SharePointHolidayRepository
from app.repositories.sharepoint.manager_assignments import (
    SharePointManagerAssignmentRepository,
)
from app.repositories.postgres.employee import PostgresEmployeeRepository
from app.repositories.postgres.holidays import PostgresHolidayRepository
from app.repositories.postgres.manager_assignments import (
    PostgresManagerAssignmentRepository,
)
from app.repositories.sharepoint.requests import SharePointRequestRepository

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"


def _unsupported(domain: str, backend: str):
    raise NotImplementedError(
        f"Storage backend '{backend}' for {domain} is not implemented. "
        f"'{SHAREPOINT}' is always valid; '{POSTGRES}' only once that domain's "
        f"cutover PR has landed its repository — keep {domain} on "
        f"'{SHAREPOINT}' until then."
    )


def get_employee_repository() -> EmployeeRepository:
    if settings.STORAGE_EMPLOYEES == SHAREPOINT:
        return SharePointEmployeeRepository()
    if settings.STORAGE_EMPLOYEES == POSTGRES:
        return PostgresEmployeeRepository()
    _unsupported("employees", settings.STORAGE_EMPLOYEES)


def get_manager_assignment_repository() -> ManagerAssignmentRepository:
    # Manager assignments live with the Staff Directory, so they follow the
    # employees flag.
    if settings.STORAGE_EMPLOYEES == SHAREPOINT:
        return SharePointManagerAssignmentRepository()
    if settings.STORAGE_EMPLOYEES == POSTGRES:
        return PostgresManagerAssignmentRepository()
    _unsupported("employees", settings.STORAGE_EMPLOYEES)


def get_holiday_repository() -> HolidayRepository:
    if settings.STORAGE_HOLIDAYS == SHAREPOINT:
        return SharePointHolidayRepository()
    if settings.STORAGE_HOLIDAYS == POSTGRES:
        return PostgresHolidayRepository()
    _unsupported("holidays", settings.STORAGE_HOLIDAYS)


def get_leave_request_repository() -> RequestRepository:
    return _request_repository(settings.SP_LIST_LEAVE_REQUESTS)


def get_overtime_request_repository() -> RequestRepository:
    return _request_repository(settings.SP_LIST_OVERTIME_REQUESTS)


def get_carryover_payout_repository() -> RequestRepository:
    return _request_repository(settings.SP_LIST_CARRYOVER_PAYOUT)


def _request_repository(list_id: str) -> RequestRepository:
    if settings.STORAGE_REQUESTS == SHAREPOINT:
        return SharePointRequestRepository(list_id)
    _unsupported("requests", settings.STORAGE_REQUESTS)
