"""Render functions for all email and approval page templates.

Each function accepts data and returns an HTML string using Jinja2 templates.
"""
from jinja2 import Environment, FileSystemLoader

_env = Environment(loader=FileSystemLoader("app/templates/emails"), autoescape=True)


def _render(template_name: str, **kwargs) -> str:
    return _env.get_template(template_name).render(**kwargs)


# --- Leave Requests ---

def render_leave_approval_email(
    fields: dict, emp_fields: dict, approve_url: str, reject_url: str,
    submitter_name: str = "", projected: dict | None = None,
) -> str:
    return _render(
        "leave_approval_email.html",
        fields=fields, emp_fields=emp_fields,
        approve_url=approve_url, reject_url=reject_url,
        submitter_name=submitter_name, projected=projected,
    )


def render_leave_approved(fields: dict, manager_name: str) -> str:
    return _render("leave_approved.html", fields=fields, manager_name=manager_name)


def render_leave_rejected(fields: dict, manager_name: str) -> str:
    return _render("leave_rejected.html", fields=fields, manager_name=manager_name)


def render_leave_balance_update(employee_name: str, balances: dict, is_next_year: bool) -> str:
    template = "leave_balance_update_next_year.html" if is_next_year else "leave_balance_update.html"
    return _render(template, employee_name=employee_name, balances=balances)


def render_leave_hourly_approved(fields: dict, manager_name: str) -> str:
    return _render("leave_hourly_approved.html", fields=fields, manager_name=manager_name)


def render_partial_day_holiday_rejected(fields: dict, holiday_name: str) -> str:
    return _render("partial_day_holiday_rejected.html", fields=fields, holiday_name=holiday_name)


def render_partial_day_halffriday_rejected(fields: dict) -> str:
    return _render("partial_day_halffriday_rejected.html", fields=fields)


def render_leave_confirmation(fields: dict, emp_fields: dict, projected: dict | None = None) -> str:
    return _render(
        "leave_confirmation.html",
        fields=fields, emp_fields=emp_fields, projected=projected,
    )


def render_bereavement_alert(fields: dict, submitter_name: str) -> str:
    return _render("bereavement_alert.html", fields=fields, submitter_name=submitter_name)


# --- Overtime Requests ---

def render_overtime_approval_email(
    fields: dict, submitter_name: str, approve_url: str, reject_url: str,
    is_half_friday: bool, emp_fields: dict | None = None, projected: dict | None = None,
) -> str:
    return _render(
        "overtime_approval_email.html",
        fields=fields, submitter_name=submitter_name,
        approve_url=approve_url, reject_url=reject_url,
        is_half_friday=is_half_friday, emp_fields=emp_fields, projected=projected,
    )


def render_overtime_approved(fields: dict, submitter_name: str, manager_name: str, balances: dict) -> str:
    return _render(
        "overtime_approved.html",
        fields=fields, submitter_name=submitter_name, manager_name=manager_name, balances=balances,
    )


def render_overtime_rejected(fields: dict, submitter_name: str, manager_name: str, balances: dict) -> str:
    return _render(
        "overtime_rejected.html",
        fields=fields, submitter_name=submitter_name, manager_name=manager_name, balances=balances,
    )


def render_overtime_confirmation(fields: dict, emp_fields: dict, projected: dict | None = None) -> str:
    return _render(
        "overtime_confirmation.html",
        fields=fields, emp_fields=emp_fields, projected=projected,
    )


def render_overtime_auto_rejected(fields: dict, holiday_name: str) -> str:
    return _render("overtime_auto_rejected.html", fields=fields, holiday_name=holiday_name)


def render_overtime_hourly_approved(fields: dict, submitter_name: str, manager_name: str) -> str:
    return _render(
        "overtime_hourly_approved.html",
        fields=fields, submitter_name=submitter_name, manager_name=manager_name,
    )


# --- CarryOver / Payout ---

def render_carryover_confirmation(
    request_id, emp_fields: dict, days: float,
    new_vacation: float, new_carryover: float, current_payout: float,
) -> str:
    return _render(
        "carryover_confirmation.html",
        request_id=request_id, emp_fields=emp_fields, days=days,
        new_vacation=new_vacation, new_carryover=new_carryover, current_payout=current_payout,
    )


def render_payout_confirmation(
    request_id, emp_fields: dict, days: float,
    new_vacation: float, current_carryover: float, new_payout: float,
) -> str:
    return _render(
        "payout_confirmation.html",
        request_id=request_id, emp_fields=emp_fields, days=days,
        new_vacation=new_vacation, current_carryover=current_carryover, new_payout=new_payout,
    )


def render_carryover_payout_approval_email(
    request_id, request_type: str, employee_name: str, days: float,
    current_vacation: float, current_carryover: float, current_payout: float,
    new_vacation: float, new_carryover: float, new_payout: float,
    approve_url: str, reject_url: str,
) -> str:
    return _render(
        "carryover_payout_approval_email.html",
        request_id=request_id, request_type=request_type,
        employee_name=employee_name, days=days,
        current_vacation=current_vacation, current_carryover=current_carryover,
        current_payout=current_payout,
        new_vacation=new_vacation, new_carryover=new_carryover, new_payout=new_payout,
        approve_url=approve_url, reject_url=reject_url,
    )


def render_carryover_approved(request_id, employee_name: str, balances: dict) -> str:
    return _render("carryover_approved.html", request_id=request_id, employee_name=employee_name, balances=balances)


def render_payout_approved(request_id, employee_name: str, balances: dict) -> str:
    return _render("payout_approved.html", request_id=request_id, employee_name=employee_name, balances=balances)


def render_carryover_rejected(request_id, fields: dict) -> str:
    return _render("carryover_rejected.html", request_id=request_id, fields=fields)


def render_payout_rejected(request_id, fields: dict) -> str:
    return _render("payout_rejected.html", request_id=request_id, fields=fields)


def render_payout_cap_rejected(request_id, emp_fields: dict) -> str:
    return _render("payout_cap_rejected.html", request_id=request_id, emp_fields=emp_fields)


def render_system_override_reject(
    request_id, emp_fields: dict, request_type: str,
    current_vacation: float, current_carryover: float, current_payout: float,
) -> str:
    return _render(
        "system_override_reject.html",
        request_id=request_id, emp_fields=emp_fields, request_type=request_type,
        current_vacation=current_vacation, current_carryover=current_carryover,
        current_payout=current_payout,
    )


def render_refund_notification(
    request_type: str, request_id, employee_name: str, fields: dict, balances: dict | None,
) -> str:
    return _render(
        "refund_notification.html",
        request_type=request_type, request_id=request_id,
        employee_name=employee_name, fields=fields, balances=balances,
    )


def render_system_override_reject_at_approval(request_id, employee_name: str, request_type: str) -> str:
    return _render(
        "system_override_reject_at_approval.html",
        request_id=request_id, employee_name=employee_name, request_type=request_type,
    )


# --- Duplicate Request ---

def render_duplicate_request_rejected(request_type: str, fields: dict, overlap: dict) -> str:
    request_type_display = "Leave" if request_type == "leave" else "Time Make-Up"
    return _render(
        "duplicate_request_rejected.html",
        request_type=request_type, request_type_display=request_type_display,
        fields=fields, overlap=overlap,
    )


# --- Dashboard ---

def render_dashboard_link_email(manager_name: str, dashboard_url: str) -> str:
    return _render("dashboard_link_email.html", manager_name=manager_name, dashboard_url=dashboard_url)
