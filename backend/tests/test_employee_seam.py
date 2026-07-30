"""Migration PR F: the employee domain reads and writes through the seam.

This is a behaviour-preserving rewire — SharePoint is still the source of
truth — so there is no new logic to unit-test. What *is* worth pinning is the
architectural property the cutover depends on: nothing outside
app/repositories/ may touch the Staff Directory list directly, or flipping
STORAGE_EMPLOYEES to "postgres" would leave those call sites still talking to
SharePoint and the two stores would silently diverge.

The scan below is the "no call site was missed" check, kept as a test so it
also catches a *future* direct call added by hand.
"""
import ast
import pathlib

import pytest

APP = pathlib.Path("app")

# Files allowed to name the Staff Directory list directly.
ALLOWED = {
    # The repository implementations — this is their whole job.
    pathlib.Path("app/repositories/sharepoint/employee.py"),
    pathlib.Path("app/repositories/sharepoint/manager_assignments.py"),
    # Where the id is defined.
    pathlib.Path("app/config.py"),
    # Startup probe: verifies Graph/SharePoint connectivity itself, rather than
    # reading employees. SharePoint keeps identity + intake, so this stays.
    pathlib.Path("app/main.py"),
}


def _python_files():
    return [p for p in APP.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_direct_staff_directory_access_outside_the_repositories():
    offenders = []
    for path in _python_files():
        if path in ALLOWED:
            continue
        if "SP_LIST_STAFF_DIRECTORY" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))

    assert not offenders, (
        "These files reach the Staff Directory list directly instead of going "
        "through get_employee_repository(); they would keep writing to "
        "SharePoint after STORAGE_EMPLOYEES is flipped:\n  "
        + "\n  ".join(sorted(offenders))
    )


def test_the_balance_engine_no_longer_imports_sp_client():
    """The balance engine is the domain core — it must be storage-agnostic.

    After this rewire it reaches employees only through the repository, which
    is what lets the employees cutover happen without touching balance logic.
    """
    source = pathlib.Path("app/services/balance.py").read_text(encoding="utf-8")
    assert "sp_client" not in source


@pytest.mark.parametrize(
    "module",
    [
        "app/services/employee.py",
        "app/services/manager_assignments.py",
        "app/services/balance.py",
        "app/services/leave_requests.py",
        "app/services/overtime_requests.py",
        "app/services/carryover_payout.py",
        "app/routes/dashboard.py",
        "app/routes/twilio.py",
        "app/tasks/carryover_reset.py",
    ],
)
def test_rewired_modules_import_the_employee_repository(module):
    """Every rewired module should obtain its repo from the factory."""
    source = pathlib.Path(module).read_text(encoding="utf-8")
    assert "get_employee_repository" in source


def test_user_information_list_is_still_read_directly():
    """Identity stays in SharePoint by design, so it is deliberately not
    behind the seam. Pinned so a later 'cleanup' does not sweep it in."""
    source = pathlib.Path("app/services/employee.py").read_text(encoding="utf-8")
    assert "User Information List" in source
    assert "sp_client" in source


def test_every_app_module_still_parses():
    """Cheap guard on a mechanical, repo-wide rewrite."""
    for path in _python_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - failure path
            pytest.fail(f"{path} does not parse: {exc}")
