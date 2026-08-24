"""Tell an employee when an approval has just stranded another of their requests.

Only approved requests reserve dates, so an employee can hold two overlapping
requests quite happily while both are pending. The moment one is approved the
other stops being approvable — and nothing marks it, nothing writes to it, and
until now nothing told anybody. The employee saw a confirmation at submission
and then silence.

This is the part of the old duplicate handling worth keeping. What is not kept
is the rejection that came with it: nothing here writes to a request or cancels
anything. The decision stays with the manager.

Reads only, and every failure is swallowed — this runs after an approval has
already been written, so it must never be able to turn a completed approval into
an error.
"""

import logging

from app.config import settings
from app.graph.email import send_email
from app.graph.sharepoint import sp_client
from app.services.overlap_detection import (
    _extract_lookup_id,
    conflict_warning,
    find_conflict_for_row,
    find_overtime_conflict_for_row,
    find_requests_blocked_by,
)

logger = logging.getLogger(__name__)

# Per request type: which list, which person column, and which row matcher.
_KINDS = {
    "leave": (settings.SP_LIST_LEAVE_REQUESTS, "SubmittedTest", find_conflict_for_row),
    "overtime": (settings.SP_LIST_OVERTIME_REQUESTS, "SubmittedBy", find_overtime_conflict_for_row),
}


async def notify_requests_blocked_by_approval(
    kind: str,
    approved_item_id: str | int,
    approved_fields: dict,
    employee_email: str,
) -> int:
    """Email the employee about each pending request this approval has stranded.

    Args:
        kind: "leave" or "overtime".
        approved_item_id: The request that was just approved.
        approved_fields: Its field values, read here for the submitter so the
            caller does not need to know which person column the list uses.
        employee_email: Where to send. Empty skips.

    Returns:
        How many notices were sent. Zero on any failure, which is logged.
    """
    if not employee_email:
        return 0

    list_id, person_column, matcher = _KINDS[kind]
    submitter_lookup_id = _extract_lookup_id(approved_fields, person_column)
    if not submitter_lookup_id:
        # Their rows cannot be told from anyone else's, so nothing to check.
        return 0
    try:
        # Fetched after the approval landed, so the approved row already reads
        # as approved and the rows it strands can be seen.
        items = await sp_client.get_list_items(list_id)
        blocked = find_requests_blocked_by(
            items, approved_item_id, submitter_lookup_id, person_column, matcher,
        )
    except Exception:  # noqa: BLE001 - the approval is already done; never undo it with an error
        logger.exception(
            "Could not check what approving %s #%s stranded", kind, approved_item_id,
        )
        return 0

    from app.templates_render import render_request_now_blocked

    sent = 0
    for item, conflict in blocked:
        item_id = item.get("id")
        try:
            html = render_request_now_blocked(
                kind, item_id, item.get("fields", {}),
                conflict_warning(conflict, kind, "employee"),
            )
            await send_email(
                to=[employee_email],
                subject=(
                    f"{'Leave' if kind == 'leave' else 'Time Make-Up'} Request "
                    f"#{item_id} Cannot Be Approved"
                ),
                html_body=html,
            )
            sent += 1
            logger.info(
                "Told the employee that %s #%s is blocked by the approval of #%s",
                kind, item_id, approved_item_id,
            )
        except Exception:  # noqa: BLE001 - one failed notice must not stop the rest
            logger.exception("Could not send the blocked-request notice for %s #%s", kind, item_id)

    return sent
