"""The requests domain reads and writes through the seam.

Behaviour-preserving rewire — SharePoint is still the source of truth — so what
is pinned is the architectural property the requests cutover depends on: nothing
outside app/repositories/ (and the SharePoint-intake machinery, which retires
with the migration) may touch a list via sp_client directly, or flipping
STORAGE_REQUESTS to "postgres" would leave those call sites still talking to
SharePoint and the two stores would silently diverge.

Same pattern as tests/test_employee_seam.py, but scanning *calls* rather than
list-id names: the SharePoint list ids legitimately remain in service code as
cross-backend domain keys (claim_action, reminder rows, the Twilio config), so
naming an id is fine — calling sp_client with one is not.
"""
import pathlib
import re

import pytest

APP = pathlib.Path("app")

# sp_client item-level calls; matching a line means direct list access.
SP_CALL = re.compile(
    r"sp_client\.(get_list_items|get_list_item_or_none|get_list_item|"
    r"create_list_item|update_list_item_fields|delete_list_item)\("
)

# Files allowed to call sp_client list methods directly.
ALLOWED = {
    # The SharePoint repository implementations — their whole job.
    pathlib.Path("app/repositories/sharepoint/employee.py"),
    pathlib.Path("app/repositories/sharepoint/holidays.py"),
    pathlib.Path("app/repositories/sharepoint/manager_assignments.py"),
    pathlib.Path("app/repositories/sharepoint/requests.py"),
    # SharePoint-intake machinery: processes Graph webhook payloads for
    # SP-created items only, and retires with the requests cutover.
    pathlib.Path("app/routes/webhooks.py"),
    pathlib.Path("app/tasks/change_processor.py"),
    pathlib.Path("app/tasks/dispatcher.py"),
    pathlib.Path("app/tasks/subscription_manager.py"),
    # Startup probe verifies Graph connectivity, not business data.
    pathlib.Path("app/main.py"),
}

# Identity reads stay direct by design: the M365 User Information List is not
# behind the seam (SharePoint keeps identity after the migration).
IDENTITY_MARKER = '"User Information List"'


def _python_files():
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_direct_list_access_outside_the_repositories():
    offenders = []
    for path in _python_files():
        if path in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SP_CALL.search(line) and IDENTITY_MARKER not in line:
                offenders.append(f"{path}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These call sp_client list methods directly instead of going through a "
        "repository; they would keep talking to SharePoint after a storage flag "
        "is flipped:\n  " + "\n  ".join(sorted(offenders))
    )


@pytest.mark.parametrize(
    "module, needed",
    [
        ("app/services/leave_requests.py", "get_leave_request_repository"),
        ("app/services/overtime_requests.py", "get_overtime_request_repository"),
        ("app/services/carryover_payout.py", "get_carryover_payout_repository"),
        ("app/services/overlap_detection.py", "get_leave_request_repository"),
        ("app/services/notify_blocked.py", "get_request_repository_for_list"),
        ("app/services/audit_trail.py", "get_request_repository_for_list"),
        ("app/tasks/reminders.py", "get_request_repository_for_list"),
        ("app/routes/dashboard.py", "get_request_repository_for_list"),
        ("app/routes/twilio.py", "get_request_repository_for_list"),
    ],
)
def test_rewired_modules_obtain_their_repo_from_the_factory(module, needed):
    source = pathlib.Path(module).read_text(encoding="utf-8")
    assert needed in source


def test_identity_reads_are_still_direct():
    """The User Information List read is deliberately outside the seam.
    Pinned so a later cleanup does not sweep it in."""
    source = pathlib.Path("app/services/leave_requests.py").read_text(encoding="utf-8")
    assert IDENTITY_MARKER in source
    assert "sp_client" in source
