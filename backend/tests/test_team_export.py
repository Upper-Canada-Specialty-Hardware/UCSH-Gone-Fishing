"""The manager History tab's team requests Excel export (#360).

A manager or admin can download the team requests on screen as an .xlsx; an
employee is refused. Driven through a TestClient so the real route body runs,
with the signed-token dependency overridden to a chosen role (the same pattern
as test_dashboard_postgres_routes.py).
"""

from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.routes.dashboard import DashboardUser, get_dashboard_user
from app.routes.team_export import router as team_export_router

# Two team requests in the shape /team/requests returns: flat SharePoint fields
# plus id, request_type, employee_name and managers.
TEAM_REQUESTS = [
    {"request_type": "leave", "id": "1", "employee_name": "Ana Reyes", "managers": "Sam Ortega",
     "Status": "Approved", "Created": "2026-10-01", "ApprovedDate": "2026-10-02",
     "LeaveType": "Vacation", "StartDate": "2026-11-02", "EndDate": "2026-11-06", "Days": 5, "Title": "trip"},
    {"request_type": "overtime", "id": "2", "employee_name": "Ben Okafor", "managers": "Sam Ortega",
     "Status": "Pending", "Created": "2026-10-03", "StartDate": "2026-10-03", "Hours": 4, "Title": "install"},
]

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _client(role: str) -> TestClient:
    """A TestClient for the export router alone, signed in as the given role.

    Args:
        role: "manager", "admin" or "employee" to stand in for the signed token.

    Returns:
        The client, with get_dashboard_user overridden to that role.
    """
    app = FastAPI()
    app.include_router(team_export_router)
    app.dependency_overrides[get_dashboard_user] = lambda: DashboardUser(role=role, user_id="42")
    return TestClient(app, raise_server_exceptions=False)


def test_a_manager_gets_an_xlsx():
    resp = _client("manager").post("/team/requests/export", json={"requests": TEAM_REQUESTS})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    assert "attachment" in resp.headers["content-disposition"]
    # The bytes really are a workbook, with the team's rows on the leave sheet.
    book = load_workbook(BytesIO(resp.content))
    assert "Leave Requests" in book.sheetnames
    assert book["Leave Requests"]["B2"].value == "Ana Reyes"   # Employee is the second column


def test_an_admin_may_also_export():
    resp = _client("admin").post("/team/requests/export", json={"requests": TEAM_REQUESTS})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE


def test_an_employee_is_refused():
    resp = _client("employee").post("/team/requests/export", json={"requests": TEAM_REQUESTS})
    assert resp.status_code == 403, resp.text
