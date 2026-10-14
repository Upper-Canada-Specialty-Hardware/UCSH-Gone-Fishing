"""The admin requests screen's Excel export (#278).

Builds one workbook from the requests the admin is looking at (the shown rows,
or the ticked ones), with one sheet per SharePoint list and every column of
that list on its sheet. Pure: it takes the request items the dashboard already
read from /admin/requests, so it is tested without SharePoint or Postgres, and
it follows the shape and styling of services/carryover_export.py.
"""

from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

# One sheet per list, in this order. Each entry: (request_type, sheet title).
SHEETS = [
    ("leave", "Leave Requests"),
    ("overtime", "Overtime Requests Data"),
    ("carryover-payout", "CarryOver_Payout_Data"),
]

# The columns shown first on each sheet, in this order: (header, item key).
# Any other field present on a row is appended after these, so nothing is lost.
COMMON_COLUMNS = [
    ("Request #", "id"),
    ("Employee", "employee_name"),
    ("Manager", "managers"),
    ("Status", "Status"),
    ("Created", "Created"),
    ("Approved date", "ApprovedDate"),
]
TYPE_COLUMNS = {
    "leave": [
        ("Leave type", "LeaveType"), ("Start date", "StartDate"),
        ("End date", "EndDate"), ("Days", "Days"), ("Title", "Title"),
    ],
    "overtime": [
        ("Date", "StartDate"), ("Hours", "Hours"), ("Details", "Title"),
    ],
    "carryover-payout": [
        ("Type", "TypeofRequest"), ("Days", "Days"),
        ("New balance", "NewBalance"), ("System state", "SystemState"),
    ],
}

# Keys never shown as their own column: the request_type routing field, the
# backend's resolved helpers already given a header above, and Graph's own
# odata annotations.
_SKIP_KEYS = {"request_type", "employee_name", "managers"}


def _is_shown_key(key: str) -> bool:
    """Whether a field key becomes a column (not routing noise or an odata hint)."""
    return key not in _SKIP_KEYS and "@" not in key and not key.startswith("odata")


def _columns_for(request_type: str, rows: list[dict]) -> list[tuple[str, str]]:
    """The ordered (header, key) columns for one list's sheet.

    Args:
        request_type: Which list this sheet is for.
        rows: The request items going on this sheet.

    Returns:
        The common columns, then the list's own columns, then any extra field
        seen on a row that none of those already covers, so every column shows.
    """
    columns = COMMON_COLUMNS + TYPE_COLUMNS.get(request_type, [])
    taken = {key for _, key in columns}                        # keys already given a header
    extras: list[str] = []
    for row in rows:
        for key in row:                                        # preserve first-seen order across rows
            if key not in taken and key not in extras and _is_shown_key(key):
                extras.append(key)
    return columns + [(key, key) for key in extras]            # extra header = its raw field name


def _cell(value):
    """A request field as a spreadsheet cell: a list of names joined, else as is."""
    if isinstance(value, list):                                # e.g. AllManagers lookup entries
        return ", ".join(
            str(v.get("LookupValue", v)) if isinstance(v, dict) else str(v) for v in value
        )
    if isinstance(value, dict):                                # a lone lookup object
        return value.get("LookupValue", str(value))
    return value


def _autosize(sheet) -> None:
    """Widen each column to its longest value, within reason (as carryover_export)."""
    for i, column in enumerate(sheet.columns, start=1):
        longest = max((len(str(c.value)) for c in column if c.value is not None), default=8)
        sheet.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 45)


def build_requests_workbook(requests: list[dict]) -> bytes:
    """Build the requests export as .xlsx bytes.

    Args:
        requests: Request items as /admin/requests returns them: each a flat
            dict of its SharePoint fields plus "id", "request_type",
            "employee_name" and "managers".

    Returns:
        The workbook file's bytes, one sheet per list that has any requests
        (an empty export still gives one sheet, so the file always opens).
    """
    by_type: dict[str, list[dict]] = {key: [] for key, _ in SHEETS}
    for item in requests:
        kind = item.get("request_type")
        if kind in by_type:
            by_type[kind].append(item)

    book = Workbook()
    book.remove(book.active)                                   # drop the default sheet; add our own below
    for request_type, title in SHEETS:
        rows = by_type[request_type]
        if not rows and any(by_type.values()):
            continue                                           # skip an empty list when others have rows
        sheet = book.create_sheet(title)
        columns = _columns_for(request_type, rows)
        sheet.append([header for header, _ in columns])
        for item in rows:
            sheet.append([_cell(item.get(key)) for _, key in columns])
        for cell in sheet[1]:
            cell.font = Font(bold=True)                        # header row
        sheet.freeze_panes = "A2"                              # header stays put when scrolling
        _autosize(sheet)

    if not book.sheetnames:                                    # nothing at all: one empty Leave sheet
        book.create_sheet("Leave Requests").append([h for h, _ in COMMON_COLUMNS])

    out = BytesIO()
    book.save(out)
    return out.getvalue()


def requests_export_filename(today: date) -> str:
    """The download's file name, dated so repeated exports do not collide."""
    return f"requests-{today.isoformat()}.xlsx"
