"""Admin legacy data handler: list reconciliation findings and resolve them (#337).

Mounted under /api/dashboard behind the admin password, like every other
/admin/* router (app/main.py). The admin legacy screen reads the open, resolved
or ignored findings with a per-case count for the badges, and marks one resolved
or ignored with a required note. Nothing here changes a balance or a request:
HR fixes the underlying record through the existing hand edit; a finding only
records that the discrepancy was seen and dealt with.

* GET  /admin/legacy/findings            - a page of findings plus per-case counts
* POST /admin/legacy/findings/{id}/resolve - mark one resolved, with a note
* POST /admin/legacy/findings/{id}/ignore  - mark one ignored, with a note
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services import legacy_findings

router = APIRouter()


class DecisionBody(BaseModel):
    """Body of a resolve or ignore: HR's note, and optionally who decided."""
    note: str
    actor: str | None = None


@router.get("/admin/legacy/findings")
async def admin_legacy_findings(
    case: str | None = Query(None),
    status: str = Query("open"),
    subject: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """List reconciliation findings and the per-case counts for the badges.

    Args:
        case: Keep only this case, or None for every case.
        status: open (default), resolved or ignored.
        subject: "request" or "staff", or None for both.
        page: 1-based page number.
        page_size: Rows per page (1 to 200).

    Returns:
        ``{"findings", "total", "page", "page_size", "counts"}``; ``counts`` is
        every case mapped to its count at the chosen status.
    """
    listing = await legacy_findings.list_findings(
        case=case, status=status, subject=subject, page=page, page_size=page_size,
    )
    listing["counts"] = await legacy_findings.counts_by_case(status=status)   # per-case badges
    return listing


@router.post("/admin/legacy/findings/{finding_id}/resolve")
async def admin_resolve_finding(finding_id: int, body: DecisionBody):
    """Mark a finding resolved, recording the note and who.

    Args:
        finding_id: The finding's id.
        body: The required note and the optional actor.

    Returns:
        The updated finding.

    Raises:
        HTTPException: 400 with no note, 404 when the finding is gone.
    """
    try:
        return await legacy_findings.resolve(finding_id, body.note, body.actor)
    except legacy_findings.FindingError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))


@router.post("/admin/legacy/findings/{finding_id}/ignore")
async def admin_ignore_finding(finding_id: int, body: DecisionBody):
    """Mark a finding ignored, recording the note and who.

    Args:
        finding_id: The finding's id.
        body: The required note and the optional actor.

    Returns:
        The updated finding.

    Raises:
        HTTPException: 400 with no note, 404 when the finding is gone.
    """
    try:
        return await legacy_findings.ignore(finding_id, body.note, body.actor)
    except legacy_findings.FindingError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error))
