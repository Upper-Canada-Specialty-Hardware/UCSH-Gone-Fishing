import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.services.leave_requests import process_new_leave_request
from app.services.overtime_requests import process_new_overtime_request
from app.services.carryover_payout import process_new_carryover_payout

logger = logging.getLogger(__name__)
router = APIRouter()


class LeaveFormData(BaseModel):
    leave_type: str
    start_date: str
    end_date: str | None = None
    employee_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    notes: str | None = None
    partial_hours: float | None = None
    submitter_email: str


class OvertimeFormData(BaseModel):
    description: str
    date: str
    hours: int
    submitter_email: str


class CarryoverPayoutFormData(BaseModel):
    type_of_request: str  # "Carry Over" or "Payout"
    days: float
    submitter_email: str


def _check_processing():
    if not settings.PROCESSING_ENABLED:
        return JSONResponse(
            status_code=503,
            content={"detail": "Processing is currently disabled"},
        )
    return None


@router.post("/leave")
async def receive_leave_form(data: LeaveFormData):
    if (resp := _check_processing()):
        return resp
    try:
        item = await process_new_leave_request(data.model_dump(), data.submitter_email)
        return {"status": "ok", "item_id": item.get("id")}
    except Exception as e:
        logger.exception("Error processing leave form")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/overtime")
async def receive_overtime_form(data: OvertimeFormData):
    if (resp := _check_processing()):
        return resp
    try:
        item = await process_new_overtime_request(data.model_dump(), data.submitter_email)
        return {"status": "ok", "item_id": item.get("id")}
    except Exception as e:
        logger.exception("Error processing overtime form")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/carryover-payout")
async def receive_carryover_payout_form(data: CarryoverPayoutFormData):
    if (resp := _check_processing()):
        return resp
    try:
        item = await process_new_carryover_payout(data.model_dump(), data.submitter_email)
        return {"status": "ok", "item_id": item.get("id")}
    except Exception as e:
        logger.exception("Error processing carryover/payout form")
        raise HTTPException(status_code=500, detail=str(e))
