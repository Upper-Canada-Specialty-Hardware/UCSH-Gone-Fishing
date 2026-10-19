"""Admin endpoint: the pending requests whose managers are all on vacation (#370).

One read-only endpoint, mounted under ``/api/dashboard/admin`` so the admin
password guard in main.py covers it like every other admin screen:

* ``GET /admin/vacation-cover`` - every pending request whose managers are all
  off today (Toronto), with who the managers are and when each is back, so an
  admin can approve or reject it from the admin dashboard through the existing
  admin approve/reject routes. When a manager is back, the request leaves this
  list on its own (it is computed live, not from a stored flag).

The decision itself (what counts as "off", which managers a request has) lives
in ``services/vacation_cover.py``, shared with the submission and reminder
routing, so the list can never disagree with who actually got asked.
"""
import logging

from fastapi import APIRouter

from app.services.vacation_cover import list_requests_awaiting_cover

logger = logging.getLogger(__name__)

router = APIRouter()             # mounted at /api/dashboard, behind admin_guard


@router.get("/admin/vacation-cover")
async def vacation_cover() -> dict:
    """The pending requests no manager can approve because all of them are off.

    Returns:
        ``{"requests": [...]}`` where each entry carries ``request_type`` and
        ``id`` (for the admin approve/reject calls), the employee's name, what the
        request is and when, and the managers with the date each is back.
    """
    requests = await list_requests_awaiting_cover()
    return {"requests": requests}
