"""Tests for the staff export workbook builder (#277).

Pure: build_workbook takes already-read Staff Directory rows, manager names and
change rows, so it needs no SharePoint or database. Each test reads the bytes
back with openpyxl and asserts the sheets, headers, values and styling.
"""
from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from app.services import staff_export as sx


def _rows() -> list[dict]:
    """Two people as the route hands them to the builder."""
    return [
        {
            "id": "601",
            "fields": {
                "Title": "Avery Example", "EmailAddress": "avery@ucsh.com",
                "Department": "Project Management", "Location": "Toronto Warden",
                "SalaryHourly": "Salary", "CurrentVacationBalance": 4.5,
                "CurrentSickDayBalance": 2, "CurrentOvertimeBalance": 0.25,
                "CarryOver": 0, "Payout": 0, "Comments": "a long note that should wrap",
            },
            "managers": ["Sam Supervisor"],
        },
        {
            "id": "602",
            "fields": {
                "Title": "Jamie Sample", "EmailAddress": "jamie@ucsh.com",
                "Department": "Warehouse", "Location": "British Columbia",
                "SalaryHourly": "Hourly BC", "CurrentVacationBalance": -1.5,
                "CurrentSickDayBalance": 4, "CurrentOvertimeBalance": 0,
                "CarryOver": 0, "Payout": 0,
            },
            "managers": [],
        },
    ]


def _open(data: bytes):
    """Load the produced workbook back for inspection."""
    return load_workbook(BytesIO(data))


def test_resolve_columns_forces_name_in_canonical_order():
    """Unknown keys drop out, name is always present, order is canonical."""
    cols = sx.resolve_columns(["carry_over", "email", "bogus"])
    assert "name" in cols                                  # name is locked on
    assert "bogus" not in cols                             # unknown dropped
    assert cols == [c for c in sx.COLUMN_ORDER if c in cols]  # canonical order


def test_staff_sheet_has_chosen_columns_and_values():
    """The Staff sheet lists the chosen columns with their values."""
    data = sx.build_workbook(_rows(), ["name", "email", "vacation"], False, [])
    ws = _open(data)[sx.STAFF_SHEET]
    assert [c.value for c in ws[1]] == ["Name", "Email", "Vacation"]  # header row
    assert ws["A2"].value == "Avery Example"
    assert ws["C2"].value == 4.5                           # stored as a number


def test_totals_row_sums_balances_and_marks_below_zero():
    """A totals row sums the balance columns and negatives are coloured."""
    data = sx.build_workbook(_rows(), ["name", "vacation"], False, [])
    ws = _open(data)[sx.STAFF_SHEET]
    # Rows: header(1), Avery(2), Jamie(3), Total(4).
    assert ws["A4"].value == "Total"
    assert ws["B4"].value == 3.0                           # 4.5 + (-1.5)
    assert ws["B3"].value == -1.5
    assert (ws["B3"].font.color.rgb or "").endswith("C2410C")  # below zero in error colour


def test_summary_sheet_groups_by_department():
    """The summary sheet has one row per department with its totals."""
    data = sx.build_workbook(_rows(), ["name"], True, [])
    ws = _open(data)[sx.SUMMARY_SHEET]
    headers = [c.value for c in ws[1]]
    assert headers[0] == "Department" and "Below zero" in headers
    depts = {ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)}
    assert depts == {"Project Management", "Warehouse"}


def test_history_sheet_present_only_when_rows_given():
    """The change-history sheet appears only when history rows are passed."""
    none = _open(sx.build_workbook(_rows(), ["name"], False, []))
    assert sx.HISTORY_SHEET not in none.sheetnames
    history = [{
        "employee_name": "Avery Example", "field": "CurrentVacationBalance",
        "old_value": 10, "new_value": 4.5, "reason": "used days",
        "changed_at": "2026-10-14T13:00:00+00:00",
    }]
    ws = _open(sx.build_workbook(_rows(), ["name"], False, history))[sx.HISTORY_SHEET]
    assert [c.value for c in ws[1]] == ["Date", "Name", "Field", "Before", "After", "Reason"]
    assert ws["C2"].value == "Vacation"                    # column name -> friendly label
    assert ws["B2"].value == "Avery Example"


def test_header_is_frozen_and_filtered():
    """Every sheet freezes and filters its header row."""
    ws = _open(sx.build_workbook(_rows(), ["name", "email"], False, []))[sx.STAFF_SHEET]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref is not None                  # Excel filters enabled


def test_filename_is_dated():
    """The download name carries the date so repeated exports do not collide."""
    assert sx.export_filename(date(2026, 10, 14)) == "staff-directory-2026-10-14.xlsx"
