import asyncio
from contextlib import asynccontextmanager


class EmployeeLockManager:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, employee_id: str | int):
        key = str(employee_id)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        async with self._locks[key]:
            yield


lock_manager = EmployeeLockManager()
