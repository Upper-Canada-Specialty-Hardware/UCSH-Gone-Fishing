"""Manager assignments served from the Postgres ``manager_assignments`` table.

The interface returns *employee records carrying their managers*, not raw edges
— that is the shape services/manager_assignments.py already consumes, and the
SharePoint implementation satisfies it by returning Staff Directory items with
``AllManagers`` inline. The Postgres employee repository synthesises the same
``AllManagers`` list from this table, so this implementation is a thin delegate
rather than a second, subtly different query.

Keeping it a delegate matters: if it built the shape independently the two
repositories could drift, and ``_extract_all_managers`` would quietly see
different data depending on which one a caller happened to use.
"""
from app.repositories.base import ManagerAssignmentRepository
from app.repositories.postgres.employee import PostgresEmployeeRepository


class PostgresManagerAssignmentRepository(ManagerAssignmentRepository):
    async def get_all_assignments(self) -> list[dict]:
        return await PostgresEmployeeRepository().get_all()
