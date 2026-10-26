"""Render functions for all email and approval page templates.

Each function accepts data and returns an HTML string using Jinja2 templates.
"""
from jinja2 import Environment, FileSystemLoader

from app.services.request_descriptions import extract_request_description

_env = Environment(loader=FileSystemLoader("app/templates/emails"), autoescape=True)

# Templates are handed the raw SharePoint `fields` dict, so the Title ->
# description split lives here as a filter rather than as an extra argument on
# every render_* function. autoescape is on, so the employee's free text is
# HTML-escaped on the way into the email.
_env.filters["request_description"] = extract_request_description

# A hidden comment carried in _layout.html. render_layout looks for it to tell
# an already-wrapped body from a bare one, and tests assert a send went out
# wrapped by checking for it in the HTML.
LAYOUT_MARKER = "ucsh-ooo-layout"


def _render(template_name: str, **kwargs) -> str:
    return _env.get_template(template_name).render(**kwargs)


def render_layout(content_html: str) -> str:
    """Wrap one email's rendered content in the shared Outlook-safe shell.

    Applied once in send_email to the full body (content plus any dashboard
    footer), so every email shares the same header band, card and footer line.

    Args:
        content_html: The inner HTML, already rendered and trusted (it comes
            from these templates, never from unescaped user input).

    Returns:
        A full HTML document around ``content_html``; the input unchanged when
        it is already wrapped, so wrapping twice is a no-op.
    """
    if LAYOUT_MARKER in content_html:                 # already wrapped: do not nest a second shell
        return content_html
    return _render("_layout.html", content=content_html)


def render_dashboard_footer(site_url: str) -> str:
    """Render the site-link footer appended to many emails.

    Args:
        site_url: The public site address; the one button points there. A blank
            value renders nothing.

    Returns:
        The footer HTML, with one Outlook-safe button and the sign-in line.
    """
    return _render("dashboard_footer.html", site_url=site_url)


def render_held_supervisor_actions(name: str, add_url: str, not_on_team_url: str) -> str:
    """The Add and Not on my team buttons for a held request's supervisor email.

    Args:
        name: The new hire's name, shown on the Add button and the note below.
        add_url: Add Employee, filled in from the held request.
        not_on_team_url: The signed public page that hands the request back.

    Returns:
        Two Outlook-safe buttons side by side and one muted line saying what
        Not on my team does.
    """
    return _render("held_supervisor_actions.html", name=name, add_url=add_url, not_on_team_url=not_on_team_url)


# --- Leave Requests ---

def render_leave_approval_email(
    fields: dict, emp_fields: dict, approve_url: str, reject_url: str,
    submitter_name: str = "", projected: dict | None = None,
    previous_snapshot: dict | None = None,
    conflict_warning: dict | None = None,
    floor_warning: dict | None = None,
) -> str:
    """The manager's approval email for one leave request.

    Args:
        fields: The request's SharePoint fields.
        emp_fields: The employee's Staff Directory fields (current balances).
        approve_url: Signed Approve link.
        reject_url: Signed Reject link.
        submitter_name: The employee's name.
        projected: Balances after approval, or None when nothing moves.
        previous_snapshot: The prior version's fields after an admin edit.
        conflict_warning: The overlap notice, or None.
        floor_warning: ``employment.floor_warning`` when approving takes the
            floor below zero (shown in red), or None.

    Returns:
        The email body HTML.
    """
    from app.services.employment import floor_warning_text, is_hourly
    return _render(
        "leave_approval_email.html",
        fields=fields, emp_fields=emp_fields,
        approve_url=approve_url, reject_url=reject_url,
        submitter_name=submitter_name, projected=projected,
        previous_snapshot=previous_snapshot,
        conflict_warning=conflict_warning,
        floor_warning=floor_warning,
        floor_warning_text=floor_warning_text(floor_warning),   # same sentence as the text message
        hourly=is_hourly(emp_fields),                     # sick-only balance table
    )


def render_leave_approved(fields: dict, manager_name: str) -> str:
    return _render("leave_approved.html", fields=fields, manager_name=manager_name)


def render_leave_rejected(fields: dict, manager_name: str) -> str:
    return _render("leave_rejected.html", fields=fields, manager_name=manager_name)


def render_leave_balance_update(
    employee_name: str, balances: dict, hourly: bool = False,
) -> str:
    """The updated-balance email sent after a leave approval.

    One template for every date (#407): the old next-year variant said deductions
    went only to Overtime and Carry Over, which is no longer true now that
    next-year dates use the normal cascade, so it is gone.

    Args:
        employee_name: The employee's name.
        balances: Balances after approval.
        hourly: hourly (#300): show sick only, the one balance they have.

    Returns:
        The email body HTML.
    """
    return _render("leave_balance_update.html", employee_name=employee_name, balances=balances, hourly=hourly)


def render_leave_hourly_approved(fields: dict, manager_name: str) -> str:
    return _render("leave_hourly_approved.html", fields=fields, manager_name=manager_name)


def render_request_result(
    heading: str, intro: str, rows: list[dict], note: str = "",
    reason: str | None = None, footer: str | None = None,
) -> str:
    """The one decision-result email body, built from a request's ledger step (#325).

    One shell for every outcome: a before/after table for the pots that moved
    (salaried and hourly sick), the vacation-taken counter for hourly vacation,
    or just a note when nothing was used; a rejection passes no rows. A rejection
    with a reason (#434) passes ``reason`` (shown as its own "Reason:" line) and,
    for an admin rejection, ``footer`` ("If you have questions, contact HR.").

    Args:
        heading: The title line, e.g. "Leave Request: Approved".
        intro: The opening sentence naming the request and who decided it.
        rows: ``[{"label", "value"}]`` lines for the balance table, or empty.
        note: A closing sentence (why nothing moved, or a rejection note), or "".
        reason: The rejection reason, shown on its own line, or None.
        footer: A closing line below everything (the HR contact line), or None.

    Returns:
        The email body HTML; the shared layout is applied by the sender.
    """
    return _render(
        "request_result.html",
        heading=heading, intro=intro, rows=rows, note=note, reason=reason, footer=footer,
    )


def render_partial_day_holiday_rejected(fields: dict, holiday_name: str, reason: str) -> str:
    return _render(
        "partial_day_holiday_rejected.html",
        fields=fields, holiday_name=holiday_name, reason=reason,
    )


def render_partial_day_halffriday_rejected(fields: dict, reason: str) -> str:
    return _render("partial_day_halffriday_rejected.html", fields=fields, reason=reason)


def render_leave_confirmation(
    fields: dict, emp_fields: dict, projected: dict | None = None,
    conflict_warning: dict | None = None,
) -> str:
    return _render(
        "leave_confirmation.html",
        fields=fields, emp_fields=emp_fields, projected=projected,
        conflict_warning=conflict_warning,
    )


def render_bereavement_alert(fields: dict, submitter_name: str) -> str:
    return _render("bereavement_alert.html", fields=fields, submitter_name=submitter_name)


# --- Overtime Requests ---

def render_overtime_approval_email(
    fields: dict, submitter_name: str, approve_url: str, reject_url: str,
    is_half_friday: bool, emp_fields: dict | None = None, projected: dict | None = None,
    previous_snapshot: dict | None = None,
    conflict_warning: dict | None = None,
) -> str:
    return _render(
        "overtime_approval_email.html",
        fields=fields, submitter_name=submitter_name,
        approve_url=approve_url, reject_url=reject_url,
        is_half_friday=is_half_friday, emp_fields=emp_fields, projected=projected,
        previous_snapshot=previous_snapshot,
        conflict_warning=conflict_warning,
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


def render_overtime_confirmation(
    fields: dict, emp_fields: dict, projected: dict | None = None,
    conflict_warning: dict | None = None,
) -> str:
    return _render(
        "overtime_confirmation.html",
        fields=fields, emp_fields=emp_fields, projected=projected,
        conflict_warning=conflict_warning,
    )


def render_overtime_auto_rejected(fields: dict, holiday_name: str, reason: str) -> str:
    return _render(
        "overtime_auto_rejected.html",
        fields=fields, holiday_name=holiday_name, reason=reason,
    )


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
    previous_snapshot: dict | None = None,
) -> str:
    return _render(
        "carryover_payout_approval_email.html",
        request_id=request_id, request_type=request_type,
        employee_name=employee_name, days=days,
        current_vacation=current_vacation, current_carryover=current_carryover,
        current_payout=current_payout,
        new_vacation=new_vacation, new_carryover=new_carryover, new_payout=new_payout,
        approve_url=approve_url, reject_url=reject_url,
        previous_snapshot=previous_snapshot,
    )


def render_carryover_approved(request_id, employee_name: str, balances: dict) -> str:
    return _render("carryover_approved.html", request_id=request_id, employee_name=employee_name, balances=balances)


def render_payout_approved(request_id, employee_name: str, balances: dict) -> str:
    return _render("payout_approved.html", request_id=request_id, employee_name=employee_name, balances=balances)


def render_carryover_rejected(request_id, fields: dict) -> str:
    return _render("carryover_rejected.html", request_id=request_id, fields=fields)


def render_payout_rejected(request_id, fields: dict) -> str:
    return _render("payout_rejected.html", request_id=request_id, fields=fields)


def render_payout_cap_rejected(request_id, emp_fields: dict, reason: str) -> str:
    return _render(
        "payout_cap_rejected.html",
        request_id=request_id, emp_fields=emp_fields, reason=reason,
    )


def render_system_override_reject(
    request_id, emp_fields: dict, request_type: str,
    current_vacation: float, current_carryover: float, current_payout: float,
    reason: str,
) -> str:
    return _render(
        "system_override_reject.html",
        request_id=request_id, emp_fields=emp_fields, request_type=request_type,
        current_vacation=current_vacation, current_carryover=current_carryover,
        current_payout=current_payout, reason=reason,
    )


def render_refund_notification(
    request_type: str, request_id, employee_name: str, fields: dict, balances: dict | None,
) -> str:
    return _render(
        "refund_notification.html",
        request_type=request_type, request_id=request_id,
        employee_name=employee_name, fields=fields, balances=balances,
    )


def render_system_override_reject_at_approval(
    request_id, employee_name: str, request_type: str, reason: str,
) -> str:
    return _render(
        "system_override_reject_at_approval.html",
        request_id=request_id, employee_name=employee_name,
        request_type=request_type, reason=reason,
    )


def render_request_now_blocked(
    request_type: str, request_id, fields: dict, conflict_warning: dict,
) -> str:
    """Render the notice sent when an approval strands another request.

    Args:
        request_type: "leave" or "overtime", picking the detail table.
        request_id: The stranded request.
        fields: Its SharePoint field values.
        conflict_warning: The employee-facing warning block.

    Returns:
        The rendered HTML body.
    """
    return _render(
        "request_now_blocked.html",
        request_type=request_type,
        request_type_display="Leave" if request_type == "leave" else "Time Make-Up",
        request_id=request_id, fields=fields, conflict_warning=conflict_warning,
    )


# --- Dashboard ---

def render_dashboard_link_email(manager_name: str, dashboard_url: str) -> str:
    return _render("dashboard_link_email.html", manager_name=manager_name, dashboard_url=dashboard_url)


# --- Annual CarryOver Reset ---

def render_carryover_reset_employee(employee_name: str, carryover_lost: float) -> str:
    return _render(
        "carryover_reset_employee.html",
        employee_name=employee_name, carryover_lost=carryover_lost,
    )


def render_carryover_reset_admin_summary(
    year: int, succeeded: list[dict], failed: list[dict],
) -> str:
    return _render(
        "carryover_reset_admin_summary.html",
        year=year, succeeded=succeeded, failed=failed,
    )


# --- Add Employee ---

def render_welcome_employee(first_name: str, email: str, site_url: str, tutorial_url: str) -> str:
    """Render the welcome email a person gets once Add Employee creates them.

    Args:
        first_name: Their first name, for the greeting.
        email: The address they sign in with, shown so they type the right one.
        site_url: The public site (the request page and sign-in).
        tutorial_url: The hosted staff tutorial.

    Returns:
        The rendered HTML body; the dashboard footer is appended by the sender.
    """
    return _render(
        "welcome_employee.html",
        first_name=first_name, email=email, site_url=site_url, tutorial_url=tutorial_url,
    )
