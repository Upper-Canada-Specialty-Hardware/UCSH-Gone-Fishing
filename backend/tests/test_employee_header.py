"""The employee record the dashboards show in their header (#205)."""

from app.routes.dashboard import _format_employee


def test_employment_type_is_salary_or_hourly():
    fields = {"Title": "Pat Q Staff", "SalaryHourly": "Hourly"}
    assert _format_employee(fields, 7)["employment_type"] == "Hourly"   # the column Add Employee fills


def test_employment_type_is_blank_when_never_set():
    assert _format_employee({"Title": "Pat Q Staff"}, 7)["employment_type"] == ""  # page shows "Not set"
