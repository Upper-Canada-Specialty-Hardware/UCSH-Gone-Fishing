"""The request forms behind the public request page, and submitting them.

The page offers the same three forms as the Microsoft Form: leave, overtime,
and carryover or payout. This module checks a submitted form and hands it to
the same ``process_new_*`` functions the Microsoft Form path and the
/api/forms endpoints use, so a request from the page becomes the same
SharePoint item, gets its days and manager the same way, and is approved
through the same email, text and dashboard channels.

Two callers:
* a signed-in employee submitting from the page (routes/self_service.py);
* a held request being released once its submitter is added to the Staff
  Directory (services/held_requests.py).
"""

import logging
from datetime import date
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from app.services.app_settings import carryover_payout_window, toronto_today, window_is_open
from app.services.carryover_payout import process_new_carryover_payout
from app.services.leave_requests import ALLOWED_LEAVE_TYPES, process_new_leave_request
from app.services.overtime_requests import process_new_overtime_request

logger = logging.getLogger(__name__)

PARTIAL_DAY = "Half Day or Partial Day Off"   # the one leave type measured in hours
HOURS_PER_DAY = 8                              # a full working day, as the balance engine counts it


class RequestFormError(ValueError):
    """A form that cannot become a request; the message is for the person."""


class RequestTypeClosed(RequestFormError):
    """A request type an admin has closed, e.g. carry-over outside its season."""


CARRYOVER_CLOSED_MESSAGE = (
    "Carry-over and payout requests are not open right now. "
    "HR opens them at the time of year they are taken."
)


def carryover_closed_message(window, today) -> str:
    """Why carry-over and payout are closed, in words for the person.

    Args:
        window: (start, end) dates, or None when no dates are set.
        today: Today in Toronto.

    Returns:
        When they open next, when they closed, or the general message.
    """
    if window is None:
        return CARRYOVER_CLOSED_MESSAGE                        # no dates set
    start, end = (f"{d:%B} {d.day}, {d.year}" for d in window)  # "November 1, 2026" on any OS
    if today < window[0]:
        return f"Carry-over and payout requests open on {start}, until {end}."
    return f"Carry-over and payout requests closed on {end}."


async def ensure_request_type_open(request_type: str) -> None:
    """Refuse a request type outside the dates an admin set (#176).

    Only carry-over/payout can be closed; leave and overtime are always open.
    Called by every way staff send a new request (request page, held request,
    /api/forms), not by approvals or the Microsoft Form, which HR opens and
    closes on the bulletin board.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".

    Raises:
        RequestTypeClosed: Outside the window, or when no window is set.
    """
    if request_type != "carryover-payout":
        return                                                 # leave and overtime: always open
    window, today = await carryover_payout_window(), toronto_today()
    if not window_is_open(window, today):
        raise RequestTypeClosed(carryover_closed_message(window, today))


def _half_hour_steps(value: float, label: str) -> float:
    """Refuse anything that is not a positive multiple of half an hour.

    Args:
        value: Hours as entered.
        label: Name used in the message.

    Returns:
        The value unchanged.

    Raises:
        ValueError: When zero, negative or not a half-hour step.
    """
    if value <= 0:
        raise ValueError(f"{label} must be more than 0.")
    if value % 0.5 != 0:
        raise ValueError(f"{label} must be in half-hour steps, such as 1.5 or 2.")
    return value


class LeaveForm(BaseModel):
    """A leave request, as the page sends it."""

    leave_type: str
    start_date: date
    end_date: date | None = None          # not used for a partial day
    partial_hours: float | None = None    # only for a partial day
    notes: str | None = None

    @field_validator("leave_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ALLOWED_LEAVE_TYPES:                   # the balance engine branches on these exact names
            raise ValueError("Choose one of the listed leave types.")
        return value

    @model_validator(mode="after")
    def _dates_and_hours(self):
        if self.leave_type == PARTIAL_DAY:
            if self.partial_hours is None:
                raise ValueError("Enter how many hours you will be away.")
            _half_hour_steps(self.partial_hours, "Hours away")
            if self.partial_hours >= HOURS_PER_DAY:
                raise ValueError("A partial day is less than 8 hours; choose a full-day leave type instead.")
            self.end_date = self.start_date                    # a partial day is one date
        else:
            if self.end_date is None:
                raise ValueError("Enter the last day you will be away.")
            if self.end_date < self.start_date:
                raise ValueError("The last day cannot be before the first day.")
            self.partial_hours = None                          # only meaningful for a partial day
        return self


class OvertimeForm(BaseModel):
    """An overtime entry, as the page sends it."""

    description: str
    date: date
    hours: float

    @field_validator("description")
    @classmethod
    def _has_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Say what the overtime was for.")
        return value.strip()

    @field_validator("hours")
    @classmethod
    def _half_hours(cls, value: float) -> float:
        return _half_hour_steps(value, "Hours")


class CarryoverPayoutForm(BaseModel):
    """A carry-over or payout request, as the page sends it."""

    type_of_request: Literal["Carry Over", "Payout"]
    days: float

    @field_validator("days")
    @classmethod
    def _positive_half_days(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Days must be more than 0.")
        if value % 0.5 != 0:
            raise ValueError("Days must be in half-day steps, such as 1 or 2.5.")
        return value


REQUEST_FORMS: dict[str, type[BaseModel]] = {
    "leave": LeaveForm,
    "overtime": OvertimeForm,
    "carryover-payout": CarryoverPayoutForm,
}


def parse_request_form(request_type: str, data: dict) -> BaseModel:
    """Check a submitted form and return it typed.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        data: The JSON body, snake_case.

    Returns:
        The matching form model.

    Raises:
        RequestFormError: Unknown type, or the first problem with the form,
            worded for the person.
    """
    model = REQUEST_FORMS.get(request_type)
    if model is None:
        raise RequestFormError("Unknown request type.")
    try:
        return model.model_validate(data or {})
    except ValidationError as e:
        first = e.errors()[0]                                  # one problem at a time is enough
        field = " ".join(str(p) for p in first.get("loc", ())).replace("_", " ").capitalize()
        if first.get("type") == "value_error":
            # Our own validators: the message is already written for the person.
            message = str(first.get("msg", "")).removeprefix("Value error, ")
        elif first.get("type") == "missing":
            message = f"{field} is required."
        else:
            message = f"{field} is not valid."                 # a bad date, number or choice
        raise RequestFormError(message) from None


async def submit_request(request_type: str, form: BaseModel, employee: dict, source: str) -> dict:
    """Create the request in SharePoint for a Staff Directory employee.

    Args:
        request_type: "leave", "overtime" or "carryover-payout".
        form: The checked form from parse_request_form.
        employee: The submitter's Staff Directory item ({"id", "fields"}).
        source: A request_submitter.SOURCE_* value for RequestSource.

    Returns:
        The created SharePoint item.

    Raises:
        RequestFormError: When the employee record has no email.
        Exception: Whatever the SharePoint write raises; callers report it.
    """
    fields = employee.get("fields", {})
    email = (fields.get("EmailAddress") or "").strip()
    if not email:
        raise RequestFormError("Your staff record has no email address. Ask your manager to fix it.")
    name = (fields.get("Title") or "").strip()
    first_name, _, last_name = name.partition(" ")             # the title is "<first> <last>"

    form_data = form.model_dump(mode="json")                   # dates as ISO strings, as SharePoint expects
    if request_type == "leave":
        form_data.update(employee_name=name, first_name=first_name, last_name=last_name)
        item = await process_new_leave_request(form_data, email, source)
    elif request_type == "overtime":
        item = await process_new_overtime_request(form_data, email, source)
    else:
        item = await process_new_carryover_payout(form_data, email, source)
    logger.info("Request page: created %s request #%s", request_type, item.get("id"))
    return item
