"""Repository factory — hands each domain the storage-backed implementation
selected by its feature flag.

Right now every flag defaults to "sharepoint" and only the SharePoint
implementations exist, so the factory always returns those (no behavior
change). Each cutover PR will add a Postgres implementation for one domain and
wire its "postgres" branch here, then the flag is flipped. Setting a flag to
"postgres" before that impl exists raises a clear error rather than silently
falling back to SharePoint.
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
from app.repositories.sharepoint.requests import SharePointRequestRepository

SHAREPOINT = "sharepoint"
POSTGRES = "postgres"


def _unsupported(domain: str, backend: str):
    raise NotImplementedError(
        f"Storage backend '{backend}' for {domain} is not implemented yet. "
        f"Postgres implementations arrive in the per-domain cutover PRs — keep "
        f"{domain} on '{SHAREPOINT}' until then."
    )


def get_employee_repository() -> EmployeeRepository:
    if settings.STORAGE_EMPLOYEES == SHAREPOINT:
        return SharePointEmployeeRepository()
    _unsupported("employees", settings.STORAGE_EMPLOYEES)


def get_manager_assignment_repository() -> ManagerAssignmentRepository:
    # Manager assignments live with the Staff Directory, so they follow the
    # employees flag.
    if settings.STORAGE_EMPLOYEES == SHAREPOINT:
        return SharePointManagerAssignmentRepository()
    _unsupported("employees", settings.STORAGE_EMPLOYEES)


def get_holiday_repository() -> HolidayRepository:
    if settings.STORAGE_HOLIDAYS == SHAREPOINT:
        return SharePointHolidayRepository()
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
