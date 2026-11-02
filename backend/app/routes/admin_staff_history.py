"""Admin endpoints to backfill the Staff Directory version history (#448).

Two endpoints, both under ``/api/dashboard/admin`` so the admin-password guard in
main.py covers them like every other admin action:

* ``POST /admin/staff-history/import`` - start the one-off backfill in the
                                         background and return at once (202).
* ``GET  /admin/staff-history/import`` - the running (or last) run's progress and
                                         result counts.

The backfill reads each staff record's SharePoint versions (the one read allowed
after the switch) and rewrites that person's ``source="sharepoint"`` history
rows. It runs in the background because it reads many records; the GET shows how
far it has got.
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.services.staff_history_import import ImportAlreadyRunning, import_progress, start_import

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/admin/staff-history/import")
async def staff_history_import_start() -> JSONResponse:
    """Start the staff history backfill in the background and return at once.

    Returns:
        202 with ``{"started": true}`` once the background task has started.

    Raises:
        HTTPException: 409 when a run is already in flight.
    """
    try:
        start_import()
    except ImportAlreadyRunning as e:
        raise HTTPException(status_code=409, detail=str(e))   # one run at a time
    return JSONResponse(content={"started": True}, status_code=202)


@router.get("/admin/staff-history/import")
async def staff_history_import_status() -> dict:
    """The current (or last) backfill run's progress and result counts.

    Returns:
        ``{"running", "started_at", "finished_at", "people_total",
        "people_done", "rows_written", "errors", "result", "error"}``.
    """
    return import_progress()
