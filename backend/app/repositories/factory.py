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
from app.repositories.postgres.requests import (
    carryover_payout_repository as _pg_carryover_payout_repository,
    leave_request_repository as _pg_leave_request_repository,
    overtime_request_repository as _pg_overtime_request_repository,
)

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
    if settings.STORAGE_REQUESTS == POSTGRES:
        return _pg_leave_request_repository()
    return _request_repository(settings.SP_LIST_LEAVE_REQUESTS)


def get_overtime_request_repository() -> RequestRepository:
    if settings.STORAGE_REQUESTS == POSTGRES:
        return _pg_overtime_request_repository()
    return _request_repository(settings.SP_LIST_OVERTIME_REQUESTS)


def get_carryover_payout_repository() -> RequestRepository:
    if settings.STORAGE_REQUESTS == POSTGRES:
        return _pg_carryover_payout_repository()
    return _request_repository(settings.SP_LIST_CARRYOVER_PAYOUT)


def get_request_repository_for_list(list_id: str) -> RequestRepository:
    """The request repository for a SharePoint list id.

    For code that is generic over the three request lists (the audit trail,
    reminder re-sends) and carries the list id as data. The SharePoint list ids
    remain the cross-backend domain keys even once Postgres serves the rows —
    re-keying them (processing_log etc.) is the cutover's own step, not this
    seam's.

    Args:
        list_id: One of the three request list ids from settings.

    Returns:
        The repository for that list's domain, per STORAGE_REQUESTS.

    Raises:
        KeyError: If the id is not one of the three request lists.
    """
    domains = {
        settings.SP_LIST_LEAVE_REQUESTS: get_leave_request_repository,
        settings.SP_LIST_OVERTIME_REQUESTS: get_overtime_request_repository,
        settings.SP_LIST_CARRYOVER_PAYOUT: get_carryover_payout_repository,
    }
    if list_id not in domains:
        raise KeyError(f"Not a request list id: {list_id}")
    return domains[list_id]()


def _request_repository(list_id: str) -> RequestRepository:
    # SharePoint branch only — the Postgres branch is selected per list above,
    # since each request list maps to a different model.
    if settings.STORAGE_REQUESTS == SHAREPOINT:
        return SharePointRequestRepository(list_id)
    _unsupported("requests", settings.STORAGE_REQUESTS)
