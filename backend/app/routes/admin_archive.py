"""Admin endpoints to archive, restore and list departed employees (#119).

Three endpoints, all under ``/api/dashboard/admin`` so the admin-password guard
in main.py covers them like every other admin write:

* ``POST /admin/staff/{id}/archive``  - archive a departed person, with a required
                                        reason. They drop out of every list, team,
                                        picker, report and approval route and can
                                        no longer make a request; their past
                                        requests and history stay on record.
* ``POST /admin/staff/{id}/restore``  - put an archived person back.
* ``GET  /admin/staff/archived``      - the archived people, for the staff screen's
                                        archived list.

Staff live in Postgres after the switch, where the archive flag is stored and the
filtering happens; the SharePoint storage mode is not a target, so the archive and
restore writes refuse it with a plain 409 (the directory is read-only there as far
as archiving is concerned).
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.repositories.staff_store import staff_in_postgres
from app.services.staff_archive import (
    ArchiveError,
    archive_employee,
    list_archived,
    restore_employee,
)
from app.services.switch_pause import SystemPaused  # the mid-switch write refusal -> 503

logger = logging.getLogger(__name__)

router = APIRouter()             # mounted at /api/dashboard, behind admin_guard

# What archiving on the SharePoint storage mode gets: it is not a target here, so
# the write is refused rather than silently doing nothing.
_SHAREPOINT_MSG = "Archiving is only available once staff live in Postgres."


class ArchiveBody(BaseModel):
    """Body of POST /admin/staff/{id}/archive: why the person is being archived."""

    reason: str                   # required, non-blank; stored on the history


async def _require_postgres() -> None:
    """Refuse the write unless staff live in Postgres (the archive target).

    Raises:
        HTTPException: 409 on the SharePoint storage mode, with a plain message.
    """
    if not await staff_in_postgres():                      # SharePoint mode is not a target
        raise HTTPException(status_code=409, detail=_SHAREPOINT_MSG)


@router.post("/admin/staff/{employee_id}/archive")
async def admin_archive_staff(employee_id: str, body: ArchiveBody):
    """Archive a departed employee from their staff panel (#119).

    Args:
        employee_id: Staff Directory id of the person to archive.
        body: The required reason.

    Returns:
        ``{"status": "archived", "employee_id", "name", "pending_requests": [...]}``.
        The pending requests are left pending on purpose and listed so the admin
        knows an admin will still need to approve or delete them.

    Raises:
        HTTPException: 503 while processing is off; 409 on SharePoint storage or
            when the person is already archived; 400 for a blank reason; 404 when
            there is no such person.
    """
    if not settings.PROCESSING_ENABLED:                    # same gate as the other staff writes
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _require_postgres()
    try:
        result = await archive_employee(employee_id, body.reason)
    except SystemPaused as e:                             # switch in progress: same 503 as the write routes
        raise HTTPException(status_code=503, detail=e.message)
    except ArchiveError as e:                              # blank reason / already archived
        # "Already archived" is a conflict; a blank reason is a bad request.
        code = 409 if "already archived" in str(e).lower() else 400
        raise HTTPException(status_code=code, detail=str(e))
    except LookupError:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"status": "archived", **result}


@router.post("/admin/staff/{employee_id}/restore")
async def admin_restore_staff(employee_id: str):
    """Restore an archived employee back into the Staff Directory (#119).

    Args:
        employee_id: Staff Directory id of the person to restore.

    Returns:
        ``{"status": "restored", "employee_id", "name"}``.

    Raises:
        HTTPException: 503 while processing is off; 409 on SharePoint storage or
            when the person is not archived; 404 when there is no such person.
    """
    if not settings.PROCESSING_ENABLED:
        raise HTTPException(status_code=503, detail="Processing is currently disabled")
    await _require_postgres()
    try:
        result = await restore_employee(employee_id)
    except SystemPaused as e:                             # switch in progress: same 503 as the write routes
        raise HTTPException(status_code=503, detail=e.message)
    except ArchiveError as e:                              # not archived
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"status": "restored", **result}


@router.get("/admin/staff/archived")
async def admin_list_archived_staff():
    """The archived people, for the staff screen's archived list (#119).

    Returns:
        ``{"archived": [...]}``; empty on the SharePoint storage mode, where
        nobody is archived.
    """
    if not await staff_in_postgres():                      # no archive concept on SharePoint
        return {"archived": []}
    return {"archived": await list_archived()}
