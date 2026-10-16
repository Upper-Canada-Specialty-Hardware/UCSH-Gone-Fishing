"""Admin endpoints to see and flip where requests are stored (#335).

Two endpoints, both under ``/api/dashboard/admin`` so the admin-password guard in
main.py covers them like every other admin write:

* ``GET  /admin/storage``      - where requests live now, the last sync runs and
                                 their counts, and how many findings are still open.
* ``POST /admin/storage/flip`` - run the final sync and switch to Postgres.

The flip is the point of no easy return in normal operation, so only an admin
with the password can trigger it.
"""
import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.sync_run import SyncRun
from app.services.app_settings import requests_storage
from app.services.storage_switch import flip_to_postgres

logger = logging.getLogger(__name__)

router = APIRouter()             # mounted at /api/dashboard, behind admin_guard

RECENT_RUN_LIMIT = 10            # how many recent sync runs the status shows


def _run_shape(run: SyncRun) -> dict:
    """One sync-run row as JSON for the status screen.

    Args:
        run: The SyncRun row.

    Returns:
        Its id, job, times, counts and error in snake_case.
    """
    return {
        "id": run.id,
        "job": run.job,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "counts": run.counts or {},
        "error": run.error,
    }


@router.get("/admin/storage")
async def storage_status() -> dict:
    """Where requests are stored, the last sync runs, and open findings.

    Returns:
        ``{"storage", "recent_runs": [...], "open_findings": int}``.
    """
    async with async_session() as session:
        runs = (await session.execute(
            select(SyncRun).order_by(SyncRun.id.desc()).limit(RECENT_RUN_LIMIT)
        )).scalars().all()
        open_findings = (await session.execute(
            select(func.count()).select_from(LegacyFinding).where(LegacyFinding.status == "open")
        )).scalar_one()
    return {
        "storage": await requests_storage(),
        "recent_runs": [_run_shape(run) for run in runs],
        "open_findings": int(open_findings),
    }


@router.post("/admin/storage/flip")
async def storage_flip() -> dict:
    """Run the final sync and switch requests to Postgres.

    Returns:
        The result of ``flip_to_postgres`` (the final-sync counts and the new
        storage).

    Raises:
        HTTPException: 409 when a sync is running and the switch cannot start.
    """
    try:
        return await flip_to_postgres()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
