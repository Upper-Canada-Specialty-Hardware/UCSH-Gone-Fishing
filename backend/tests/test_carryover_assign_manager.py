"""Manager assignment on a carry-over or payout request (#199).

The carry-over/payout list has no AllManagers column, and SharePoint refuses a
whole update that names a column the list lacks. The assignment must therefore
write only columns the list has, so ManagerLookupId lands and the approval
pipeline runs.
"""

import asyncio

from app.services import carryover_payout

EMPLOYEE = {
    "id": "97",
    "fields": {
        "EmailAddress": "avery@ucsh.com",
        "AllManagers": [{"LookupId": "74", "LookupValue": "Sam Supervisor"}],   # set on the Staff Directory
    },
}
MANAGER = {"id": "12", "fields": {"Title": "Sam Supervisor", "EmailAddress": "sam@ucsh.com"}}


def test_the_assignment_writes_only_columns_the_list_has(monkeypatch):
    writes, pipeline = [], []

    async def employee_by_email(email):
        return EMPLOYEE

    async def manager_for(employee):
        return MANAGER

    async def lookup_id(email):
        return 74

    async def update(list_id, item_id, fields):
        writes.append(fields)

    async def run_pipeline(request_id):
        pipeline.append(request_id)

    monkeypatch.setattr(carryover_payout, "get_employee_by_email", employee_by_email)
    monkeypatch.setattr(carryover_payout, "get_manager_for_employee", manager_for)
    monkeypatch.setattr(carryover_payout, "_resolve_user_lookup_id", lookup_id)
    monkeypatch.setattr(carryover_payout.request_store, "update_list_item_fields", update)
    monkeypatch.setattr(carryover_payout, "run_approval_pipeline", run_pipeline)

    asyncio.run(carryover_payout.auto_assign_manager("257", "avery@ucsh.com"))

    assert writes == [{
        "SystemState": "Not Processed",
        "EmployeeID": 97,
        "ManagerID": 12,
        "ManagerLookupId": 74,
    }]                                                          # no AllManagers, even though the employee has some
    assert pipeline == ["257"]                                  # the approval pipeline still runs
