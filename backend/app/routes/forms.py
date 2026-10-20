import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.config import settings
from app.services.leave_requests import process_new_leave_request
from app.services.overtime_requests import process_new_overtime_request
from app.services.carryover_payout import process_new_carryover_payout
from app.services.request_intake import RequestTypeClosed, ensure_request_type_open
from app.services.request_submitter import SOURCE_API, UnknownSubmitter
from app.services.switch_pause import PAUSE_MESSAGE, is_paused

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
    hours: float
    submitter_email: str

    @field_validator("hours")
    @classmethod
    def hours_must_be_positive_half_hour(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("hours must be greater than 0")
        if v % 0.5 != 0:
            raise ValueError("hours must be a multiple of 0.5")
        return v


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


async def _check_paused():
    """A 503 with the friendly message while the storage switch runs, else None.

    New submissions are refused for the switch's short duration so a request is
    never created on the backend that is about to be left behind (#335, finding 5).

    Returns:
        A 503 JSONResponse when the switch pause is on, otherwise None.
    """
    if await is_paused():
        return JSONResponse(status_code=503, content={"detail": PAUSE_MESSAGE})
    return None


@router.post("/leave")
async def receive_leave_form(data: LeaveFormData):
    if (resp := _check_processing()):
        return resp
    if (resp := await _check_paused()):                          # refuse new requests mid-switch
        return resp
    try:
        item = await process_new_leave_request(data.model_dump(), data.submitter_email, SOURCE_API)
        return {"status": "ok", "item_id": item.get("id")}
    except UnknownSubmitter as e:
        raise HTTPException(status_code=400, detail=str(e))     # no staff record: refused, no row
    except Exception as e:
        logger.exception("Error processing leave form")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/overtime")
async def receive_overtime_form(data: OvertimeFormData):
    if (resp := _check_processing()):
        return resp
    if (resp := await _check_paused()):                          # refuse new requests mid-switch
        return resp
    try:
        item = await process_new_overtime_request(data.model_dump(), data.submitter_email, SOURCE_API)
        return {"status": "ok", "item_id": item.get("id")}
    except UnknownSubmitter as e:
        raise HTTPException(status_code=400, detail=str(e))     # no staff record: refused, no row
    except Exception as e:
        logger.exception("Error processing overtime form")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/carryover-payout")
async def receive_carryover_payout_form(data: CarryoverPayoutFormData):
    if (resp := _check_processing()):
        return resp
    if (resp := await _check_paused()):                          # refuse new requests mid-switch
        return resp
    try:
        await ensure_request_type_open("carryover-payout")     # closed outside the season (#203)
    except RequestTypeClosed as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        item = await process_new_carryover_payout(data.model_dump(), data.submitter_email, SOURCE_API)
        return {"status": "ok", "item_id": item.get("id")}
    except UnknownSubmitter as e:
        raise HTTPException(status_code=400, detail=str(e))     # no staff record: refused, no row
    except Exception as e:
        logger.exception("Error processing carryover/payout form")
        raise HTTPException(status_code=500, detail=str(e))
