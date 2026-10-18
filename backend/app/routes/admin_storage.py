"""Admin endpoints to see and flip where requests are stored (#335).

Two endpoints, both under ``/api/dashboard/admin`` so the admin-password guard in
main.py covers them like every other admin write:

* ``GET  /admin/storage``      - where requests and staff live now, the last sync
                                 and recent runs with their counts, how many
                                 requests are in Postgres, open findings, and
                                 whether the switch pause is on right now.
* ``POST /admin/storage/flip`` - run the final sync, pause writes for its
                                 duration, and switch requests (and staff) to
                                 Postgres. One-way in normal operation.

The flip is the point of no easy return in normal operation, so only an admin
with the password can trigger it, and it refuses a second run once requests
already live in Postgres.
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.database import async_session
from app.models.legacy_finding import LegacyFinding
from app.models.request_record import RequestRecord
from app.models.sync_run import SyncRun
from app.services.app_settings import requests_storage, staff_storage
from app.services.storage_switch import AlreadySwitched, SwitchRunning, start_switch, switch_status
from app.services.switch_pause import is_paused

logger = logging.getLogger(__name__)

router = APIRouter()             # mounted at /api/dashboard, behind admin_guard

RECENT_RUN_LIMIT = 10            # how many recent sync runs the status shows
SYNC_JOBS = ("shadow_sync", "final_sync", "import")   # the jobs that count as "a sync" for last_sync


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
    """Where requests and staff are stored, the last sync, counts and findings.

    Returns:
        ``{"requests_storage", "staff_storage", "paused", "last_sync",
        "recent_runs", "request_counts", "open_findings"}``. ``last_sync`` is the
        most recent import/shadow/final run (time and counts) or None;
        ``request_counts`` is how many requests of each kind are in Postgres.
    """
    async with async_session() as session:
        runs = (await session.execute(
            select(SyncRun).order_by(SyncRun.id.desc()).limit(RECENT_RUN_LIMIT)
        )).scalars().all()
        last_sync = (await session.execute(
            select(SyncRun).where(SyncRun.job.in_(SYNC_JOBS))
            .order_by(SyncRun.id.desc()).limit(1)
        )).scalars().first()
        open_findings = (await session.execute(
            select(func.count()).select_from(LegacyFinding).where(LegacyFinding.status == "open")
        )).scalar_one()
        kind_rows = (await session.execute(
            select(RequestRecord.kind, func.count()).group_by(RequestRecord.kind)
        )).all()
    return {
        "requests_storage": await requests_storage(),           # "sharepoint" or "postgres"
        "staff_storage": await staff_storage(),                 # kept in step with requests by the flip
        "paused": await is_paused(),                            # true only while a switch is mid-flight
        "switch": switch_status(),                              # the background flip's state, step and result
        "last_sync": _run_shape(last_sync) if last_sync else None,
        "recent_runs": [_run_shape(run) for run in runs],
        "request_counts": {kind: int(n) for kind, n in kind_rows},   # requests now in Postgres, per kind
        "open_findings": int(open_findings),
    }


@router.post("/admin/storage/flip")
async def storage_flip() -> JSONResponse:
    """Start the switch in the background and return at once.

    The flip runs the final sync and settles over a minute or two, so it runs as a
    background task; this validates it can start and returns 202. The storage status
    (``GET /admin/storage``, the ``switch`` key) shows its progress and result.

    Returns:
        202 with ``{"started": true}`` once the switch task has started.

    Raises:
        HTTPException: 409 when the switch is already running, or when requests and
            staff already live in Postgres ("Already switched").
    """
    try:
        await start_switch()
    except AlreadySwitched as e:
        raise HTTPException(status_code=409, detail=str(e))     # both already in Postgres
    except SwitchRunning as e:
        raise HTTPException(status_code=409, detail=str(e))     # a flip is already in progress
    return JSONResponse(content={"started": True}, status_code=202)
