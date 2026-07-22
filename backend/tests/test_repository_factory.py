"""Tests for the repository seam factory (PR C, commit 1).

Confirms (a) the factory hands back SharePoint implementations by default so
behavior is unchanged, (b) flipping a domain's flag to an unimplemented backend
fails loudly instead of silently falling back, and (c) the interfaces are
implementable without SharePoint (a fake repo) — which is what lets the later
Postgres impls and service tests decouple from Graph.
"""
import asyncio

from app.config import settings
from app.repositories import (
    get_carryover_payout_repository,
    get_employee_repository,
    get_holiday_repository,
    get_leave_request_repository,
    get_manager_assignment_repository,
    get_overtime_request_repository,
)
from app.repositories.base import (
    EmployeeRepository,
    HolidayRepository,
    ManagerAssignmentRepository,
    RequestRepository,
)
from app.repositories.sharepoint.employee import SharePointEmployeeRepository


def test_factory_returns_sharepoint_impls_by_default():
    assert isinstance(get_employee_repository(), SharePointEmployeeRepository)
    assert isinstance(get_employee_repository(), EmployeeRepository)
    assert isinstance(get_holiday_repository(), HolidayRepository)
    assert isinstance(get_leave_request_repository(), RequestRepository)
    assert isinstance(get_overtime_request_repository(), RequestRepository)
    assert isinstance(get_carryover_payout_repository(), RequestRepository)
    assert isinstance(get_manager_assignment_repository(), ManagerAssignmentRepository)


def test_request_repos_target_the_right_lists():
    assert get_leave_request_repository()._list_id == settings.SP_LIST_LEAVE_REQUESTS
    assert get_overtime_request_repository()._list_id == settings.SP_LIST_OVERTIME_REQUESTS
    assert get_carryover_payout_repository()._list_id == settings.SP_LIST_CARRYOVER_PAYOUT


def test_postgres_flag_raises_until_impl_exists():
    original = settings.STORAGE_EMPLOYEES
    settings.STORAGE_EMPLOYEES = "postgres"
    try:
        raised = False
        try:
            get_employee_repository()
        except NotImplementedError:
            raised = True
        assert raised, "flipping a flag to an unimplemented backend must fail loudly"
    finally:
        settings.STORAGE_EMPLOYEES = original


class _FakeEmployeeRepository(EmployeeRepository):
    """A SharePoint-free EmployeeRepository — proves the ABC is fully
    implementable, and models how the Postgres impl / service tests will work."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def get_all(self):
        return self._rows

    async def get_by_id(self, item_id):
        return next((r for r in self._rows if str(r["id"]) == str(item_id)), None)

    async def get_by_name(self, name):
        return next((r for r in self._rows if r["fields"]["Title"] == name), None)

    async def get_by_email(self, email):
        return next((r for r in self._rows if r["fields"]["EmailAddress"] == email), None)

    async def update_fields(self, item_id, fields):
        row = await self.get_by_id(item_id)
        row["fields"].update(fields)
        return row


def test_interface_is_implementable_without_sharepoint():
    repo = _FakeEmployeeRepository(
        [{"id": "1", "fields": {"Title": "Jo Worker", "EmailAddress": "jo@ucsh.ca"}}]
    )
    found = asyncio.run(repo.get_by_email("jo@ucsh.ca"))
    assert found["fields"]["Title"] == "Jo Worker"
