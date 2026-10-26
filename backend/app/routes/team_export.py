"""The manager History tab's team requests Excel export (#360).

The manager dashboard's History tab can download the requests it is showing as
an .xlsx, scoped to the manager's own team. This mirrors the admin export
(routes/admin_requests.py POST /admin/requests/export) and reuses the same
services/requests_export.build_requests_workbook, but it authenticates
differently: the route is a /team path, so the admin-password guard in
app/main.py does not apply, and the signed dashboard-token AuthUser dependency
gates it to a manager or admin instead.

It is team-scoped by its data, not by a server-side filter: the manager History
tab only ever holds rows from /team/requests (already narrowed to the manager's
team), so the rows the client posts here are that team's rows. The route reads
no live data and formats exactly what it is given, so it works at any time, the
same way the admin export does.

Kept in its own module so the large dashboard router does not grow, as the admin
requests endpoints are.
"""

import logging
from datetime import date

from fastapi import APIRouter
from fastapi.responses import Response

# AuthUser validates the signed dashboard token; _require_role gates the role.
# Both live in the dashboard router and are imported by name, exactly as
# routes/admin_requests.py imports that module's helpers.
from app.routes.dashboard import AuthUser, _require_role
from app.services.requests_export import RequestsExportBody, build_requests_workbook, requests_export_filename

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/team/requests/export")
async def team_export_requests(user: AuthUser, body: RequestsExportBody):
    """An .xlsx of the team requests the manager chose in the export dialog (#360, #488).

    The dialog builds the sheets from the rows the manager dashboard already
    holds, so this reads no live data. The rows are this manager's team's
    because they come from /team/requests; the route only writes them.

    Args:
        user: The signed-in dashboard user; must be a manager or admin.
        body: sheets - each with its title, columns and rows, as previewed.

    Returns:
        The workbook as an attachment.

    Raises:
        HTTPException: 401 for a bad or missing token (the dependency); 403 when
            the role is not manager or admin.
    """
    _require_role(user, "manager", "admin")              # employees cannot export the team's requests
    data = build_requests_workbook(body.sheets)          # the same writer the admin export uses
    filename = requests_export_filename(date.today())    # dated, so repeated exports do not collide
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
