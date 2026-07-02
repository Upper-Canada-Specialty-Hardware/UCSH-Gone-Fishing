"""Data-access seam. Import the factory functions from here to obtain a
storage-backed repository for a domain, e.g.:

    from app.repositories import get_employee_repository
    repo = get_employee_repository()
    emp = await repo.get_by_email(email)
"""
from app.repositories.factory import (
    get_carryover_payout_repository,
    get_employee_repository,
    get_holiday_repository,
    get_leave_request_repository,
    get_manager_assignment_repository,
    get_overtime_request_repository,
)

__all__ = [
    "get_employee_repository",
    "get_manager_assignment_repository",
    "get_holiday_repository",
    "get_leave_request_repository",
    "get_overtime_request_repository",
    "get_carryover_payout_repository",
]
