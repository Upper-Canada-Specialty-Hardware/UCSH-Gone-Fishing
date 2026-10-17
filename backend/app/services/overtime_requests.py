import logging
from datetime import date

from app.config import settings
from app.graph.sharepoint import sp_client
from app.graph.email import send_email, send_email_with_dashboard
from app.services.sms import send_sms
from app.services.employee import (
    get_employee_by_email,
    get_employee_by_id,
    get_all_managers_for_employee,
    map_location_to_province,
)
# Who a request is for: SubmitterEmail first, then the SubmittedBy person column.
from app.services.request_submitter import (
    UnknownSubmitter,
    resolve_request_submitter,
    submitter_columns,
)
from app.services.holidays import (
    get_holidays_for_province,
    get_half_friday_season,
    is_half_friday,
    is_company_holiday,
    _parse_date,
)
from app.services.balance import (
    apply_vacation_offset,
    cascade_current_year,
    recalculate_request_allow_date,
    simulate_overtime_impact,
)
from app.services.concurrency import lock_manager
from app.services.employment import overtime_costs_balance
from app.services.idempotency import approval_email_action, claim_action, release_claim
from app.services.notify_blocked import notify_requests_blocked_by_approval
from app.services.overlap_detection import (
    conflict_warning,
    find_overtime_approval_conflict,
    find_overtime_conflict_for_request,
)
from app.services.approval_links import generate_approval_url
from app.services.approval_versions import (
    bump_and_snapshot,
    get_current_version,
    get_previous_snapshot,
    MATERIAL_FIELDS_OVERTIME,
)
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.audit_trail import (
    AuditTrailBuilder,
    snapshot_balances,
    describe_cascade_changes,
    extract_approval_deltas,
    write_audit_log,
)
from app.repositories.request_store import request_store, requests_in_postgres
from app.services.switch_pause import raise_if_paused  # refuse writes while the storage switch runs

logger = logging.getLogger(__name__)


async def process_new_overtime_request(
    form_data: dict, submitter_email: str, source: str | None = None,
    notify: bool = True,
) -> dict:
    """Create the SharePoint overtime item, assign its manager, ask for approval.

    Args:
        form_data: The overtime form (description, date, hours).
        submitter_email: Who the request is for.
        source: Where it came from (a request_submitter.SOURCE_* value),
            written to RequestSource when those columns are enabled.
        notify: True asks the manager for approval, as a normal submission
            does. False assigns the manager but skips that email, so an admin
            can create and approve the request in one step (#283).

    Returns:
        The created SharePoint item.

    Raises:
        SystemPaused: While the storage switch is running (no request is created).
    """
    await raise_if_paused()                                      # no new request mid-switch
    hours = float(form_data.get("hours", 0))
    fields = {
        "Title": form_data.get("description", ""),
        "StartDate": form_data["date"],
        "Hours": hours,
        "Status": "Pending",
    }

    lookup_id = await _resolve_user_lookup_id(submitter_email)
    if lookup_id:
        fields["SubmittedByLookupId"] = lookup_id
    # SubmitterEmail and RequestSource, for submitters the person column misses.
    fields.update(submitter_columns(submitter_email, source))

    # Once requests live in Postgres the typed row needs the submitter's staff id
    # (employee_id is not null), so resolve by email now and refuse a request that
    # matches nobody rather than writing a half-formed row (#324). SharePoint mode
    # is unchanged: there EmployeeID is not written on the overtime list.
    if await requests_in_postgres():
        submitter = await get_employee_by_email(submitter_email)
        if not submitter:
            raise UnknownSubmitter(
                f"No staff record was found for {submitter_email}, so the request was not created."
            )
        fields["EmployeeID"] = str(submitter["id"])             # stamps employee_id on the typed row

    # No duplicate check here, deliberately — a clash with an already-approved
    # entry is raised when a manager tries to approve. See the module docstring
    # in app/services/overlap_detection.py.

    item = await request_store.create_list_item(settings.SP_LIST_OVERTIME_REQUESTS, fields)
    item_id = item["id"]
    logger.info("Created overtime request #%s", item_id)

    # Auto-assign manager
    await auto_assign_manager(item_id, submitter_email, notify=notify)   # notify=False skips the approval email
    return item


async def auto_assign_manager(request_id: str | int, submitter_email: str | None = None, notify: bool = True):
    """Look up submitter → supervisor → assign manager → trigger approval.

    Args:
        request_id: The overtime item.
        submitter_email: Fallback when the person column misses the submitter.
        notify: True sends the approval email at the end; False stops after the
            manager is assigned, for an admin-created-and-approved request.
    """
    # Once requests and staff both live in Postgres, the submission module records
    # the request and QUEUES its manager email, text and the receipt, instead of
    # resolving the SharePoint person columns and sending here (#324, #326).
    from app.services.request_submission import submission_active, submit
    if await submission_active():
        await submit("overtime", request_id, notify=notify)
        return

    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    # Resolve submitter from SubmittedBy Person/Group field
    employee = await resolve_request_submitter(fields, "SubmittedBy")
    if not employee and submitter_email:
        from app.services.employee import get_employee_by_email
        employee = await get_employee_by_email(submitter_email)
    if not employee:
        logger.warning("Cannot assign manager — employee not found for OT #%s", request_id)
        return

    managers = await get_all_managers_for_employee(employee)
    if not managers:
        return

    # Assign primary manager (first) to SP item
    manager = managers[0]
    mgr_fields = manager["fields"]
    mgr_email = mgr_fields.get("EmailAddress", "")
    manager_lookup_id = await _resolve_user_lookup_id(mgr_email)

    update = {"Status": "Pending"}
    if manager_lookup_id:
        update["ManagerLookupId"] = manager_lookup_id

    await request_store.update_list_item_fields(settings.SP_LIST_OVERTIME_REQUESTS, request_id, update)
    logger.info("Assigned manager %s to overtime request #%s", mgr_fields.get("Title"), request_id)

    # Trigger approval pipeline with all managers, unless an admin approves it.
    if notify:
        await send_approval_email(request_id, employee, managers)
    else:
        # Admin create-and-approve: suppress the manager email now and durably,
        # so the dispatcher's later change-notice call sends nothing (#294 B1).
        await _suppress_approval_notification(request_id, fields)


async def _suppress_approval_notification(request_id: str | int, fields: dict) -> None:
    """Claim the approval email without sending it, so no later call sends one.

    Mirrors send_approval_email's once-per-version guard: take the same version
    and claim ``approval_email_v<version>`` without sending, so the dispatcher's
    send_approval_email for this item finds the claim taken and stops. Undone by
    notify_manager_after_failed_admin_approval if the admin's approval fails.

    Args:
        request_id: The overtime item.
        fields: Its fields, already read, holding the material columns the
            version snapshot is taken from (Hours, StartDate, Title).
    """
    version = await bump_and_snapshot(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, fields, MATERIAL_FIELDS_OVERTIME,
    )                                                           # same version send_approval_email would use
    await claim_action(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, approval_email_action(version),
    )                                                           # mark it sent, so no real send follows


async def notify_manager_after_failed_admin_approval(request_id: str | int) -> bool:
    """Undo the suppressed approval email and ask the manager, after an admin's
    create-and-approve did not approve the request (#294 S3).

    Turns an admin-created overtime request the admin could not approve into an
    ordinary Pending one the manager is emailed about. Does nothing when the
    request is no longer Pending, so a terminal decision is not reopened.

    Args:
        request_id: The overtime item.

    Returns:
        True when the manager was asked; False when it was not in a state to ask.
    """
    item = await request_store.get_list_item_or_none(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    if item is None or item["fields"].get("Status") != "Pending":
        return False                                            # gone or already decided: leave it
    fields = item["fields"]
    version = await get_current_version(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    await release_claim(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, approval_email_action(version),
    )                                                           # give the claim back so the send can run
    employee = await resolve_request_submitter(fields, "SubmittedBy")
    if not employee:
        return False                                            # cannot resolve who it is for
    managers = await get_all_managers_for_employee(employee)
    if not managers:
        return False                                            # nobody to ask
    await send_approval_email(request_id, employee, managers)   # ask the manager the normal way
    return True


async def send_approval_email(request_id: str | int, employee: dict, managers: list[dict], is_reminder: bool = False):
    """Holiday check, half-friday detection, send approval email to all managers."""
    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return

    emp_fields = employee["fields"]

    location = emp_fields.get("Location", "")
    province = map_location_to_province(location)
    holidays = await get_holidays_for_province(province)
    half_friday_season = get_half_friday_season(holidays)

    overtime_date = _parse_date(fields.get("StartDate"))
    if not overtime_date:
        return

    submitter_name = emp_fields.get("Title", "")

    # Holiday check — auto-reject
    is_holiday, holiday_name = is_company_holiday(overtime_date, holidays)
    if is_holiday:
        from app.services.auto_reject_titles import append_auto_reject_tag
        reason = f"{overtime_date} is the company holiday {holiday_name}."
        await request_store.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {
                "Title": append_auto_reject_tag(fields.get("Title", ""), reason),
                "Status": "Rejected",
            },
        )
        from app.templates_render import render_overtime_auto_rejected
        html = render_overtime_auto_rejected(fields, holiday_name, reason)
        recipients = [emp_fields.get("EmailAddress", "")]
        for mgr in managers:
            mgr_email = mgr["fields"].get("EmailAddress", "")
            if mgr_email:
                recipients.append(mgr_email)
        await send_email(
            to=recipients,
            subject="Overtime Request - Auto Rejected",
            html_body=html,
        )
        logger.info("Auto-rejected overtime #%s — holiday: %s", request_id, holiday_name)
        return

    # Half-friday detection
    is_hf = is_half_friday(overtime_date, half_friday_season)

    from app.templates_render import render_overtime_approval_email, render_overtime_confirmation

    # Does this clash with an absence the employee already has approved?
    # Worked out once here and shown to both audiences: the manager, so the
    # Approve button is not presented as the obvious next click when it cannot
    # work, and the employee, who otherwise hears nothing at all until someone
    # chases it. Nothing is written and nothing is rejected - the decision
    # stays with the manager.
    #
    # Costs one list read per approval email. A failure here drops the warning,
    # never the email: an unsent approval is far worse than a missing notice.
    try:
        conflict = await find_overtime_conflict_for_request(request_id, fields)
    except Exception:  # noqa: BLE001 - a failed check must not stop the email
        logger.exception("Could not check overtime request #%s for conflicts", request_id)
        conflict = None
    manager_warning = conflict_warning(conflict, "overtime", "manager")
    employee_warning = conflict_warning(conflict, "overtime", "employee")

    # Compute projected balances
    hours = float(fields.get("Hours", 0) or 0)
    projected = simulate_overtime_impact(emp_fields, hours)

    subject = f"Overtime Request - {submitter_name}"
    if is_hf:
        subject += " - Half-Day Friday Detected"
    if is_reminder:
        subject = "Reminder: " + subject

    version = await bump_and_snapshot(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, fields, MATERIAL_FIELDS_OVERTIME,
        force_bump=is_reminder,
    )
    previous_snapshot = (
        await get_previous_snapshot(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
        if version > 1 and not is_reminder else None
    )

    # One approval email per version. The request page sends it as it creates
    # the request; SharePoint's change notice for that same new item then reaches
    # the dispatcher seconds later and asks again. Whoever claims the version first
    # sends; a later call for the same version stops here. A real change or a
    # reminder makes a new version, so those still send.
    sent_key = approval_email_action(version)
    if not await claim_action(settings.SP_LIST_OVERTIME_REQUESTS, request_id, sent_key):
        logger.info("Approval email for overtime request #%s version %s already sent; not sending again", request_id, version)
        return
    try:
        for manager in managers:
            mgr_fields = manager["fields"]
            manager_id = manager["id"]

            approve_url = generate_approval_url("overtime", request_id, "approve", manager_id, approval_version=version)
            reject_url = generate_approval_url("overtime", request_id, "reject", manager_id, approval_version=version)

            html = render_overtime_approval_email(
                fields, submitter_name, approve_url, reject_url, is_hf,
                emp_fields=emp_fields, projected=projected,
                previous_snapshot=previous_snapshot,
                conflict_warning=manager_warning,
            )

            await send_email_with_dashboard(
                to=[mgr_fields.get("EmailAddress", "")],
                subject=subject,
                html_body=html,
                primary_employee_id=manager_id,
                reply_to=emp_fields.get("EmailAddress", ""),       # replies reach the employee
            )

            # Send SMS to manager if they have a cell number (skipped on reminders)
            cell = mgr_fields.get("CellNumber", "")
            if cell and not is_reminder:
                ot_date = overtime_date.strftime("%b %d, %Y") if overtime_date else fields.get("StartDate", "")[:10]
                if projected:
                    bal_line = f"If approved: MU: {projected['CurrentOvertimeBalance']}.\n"
                else:
                    bal_line = ""
                # A failed text must not undo the emails: raising here would release
                # the claim and let the next caller email this manager again.
                try:
                    await send_sms(
                        to=cell,
                        body=(
                            f"Time Make-Up Request #{request_id} for {submitter_name} ({hours} hrs).\n"
                            f"{ot_date}\n"
                            f"{bal_line}"
                            f"Reply \"OT Approve {request_id}\" or \"OT Reject {request_id}\""
                        ),
                    )
                except Exception:  # noqa: BLE001 - logged; the email already went out
                    logger.exception("Could not text the manager about overtime request #%s", request_id)

        logger.info("Sent approval email for overtime #%s to %d manager(s)", request_id, len(managers))

        # Send confirmation email to employee (not on reminders - already received once)
        emp_email = emp_fields.get("EmailAddress", "")
        if emp_email and not is_reminder:
            html = render_overtime_confirmation(
                fields, emp_fields, projected, conflict_warning=employee_warning,
            )
            await send_email_with_dashboard(
                to=[emp_email],
                subject=f"Time Make-Up Request Received - {submitter_name}",
                html_body=html,
                primary_employee_id=employee["id"],
            )
    except BaseException:                                       # cancellation too (a redeploy mid-send)
        # The send failed: drop the claim so the next change notice or reminder can retry.
        try:
            await release_claim(settings.SP_LIST_OVERTIME_REQUESTS, request_id, sent_key)
        except Exception:  # noqa: BLE001 - keep the send error, which is the one that matters
            logger.exception("Could not release the approval email claim for #%s", request_id)
        raise


async def admin_edit_overtime_request(
    request_id: str | int,
    payload: dict,
    reason: str,
) -> dict:
    """Apply an admin-driven edit to a pending overtime request, re-send approval email."""
    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": f"Request is not Pending (current status: {fields.get('Status')})"}

    try:
        new_hours = float(payload["Hours"])
    except (KeyError, ValueError, TypeError):
        return {"error": "Hours must be a number"}
    if new_hours <= 0:
        return {"error": "Hours must be > 0"}

    new_start = payload.get("StartDate", "")
    if not _parse_date(new_start):
        return {"error": "StartDate is required (ISO date)"}

    new_title = payload.get("Title", "")
    if not isinstance(new_title, str):
        return {"error": "Title must be a string"}

    employee = await resolve_request_submitter(fields, "SubmittedBy")
    if not employee:
        return {"error": "Cannot resolve employee for this request"}
    employee_id = employee["id"]

    before = {
        "Hours": fields.get("Hours"),
        "StartDate": fields.get("StartDate"),
        "Title": fields.get("Title"),
    }
    after = {
        "Hours": new_hours,
        "StartDate": new_start,
        "Title": new_title,
    }

    # Once requests and staff live in Postgres the edit is applied in one
    # transaction: bump the approval version (old links go stale), record the field
    # changes, append an "edited" event and queue fresh manager links through the
    # outbox (#324). SharePoint mode keeps the send path below.
    from app.services.request_submission import apply_admin_edit, submission_active
    if await submission_active():
        return await apply_admin_edit("overtime", request_id, before, after, reason)

    async with lock_manager.lock(employee_id):
        fresh = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
        if fresh["fields"].get("Status") != "Pending":
            return {"error": "Request status changed during edit — refresh and try again"}

        await request_store.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {
                "Hours": new_hours,
                "StartDate": new_start,
                "Title": new_title,
            },
        )

        managers = await get_all_managers_for_employee(employee)
        if not managers:
            logger.warning("Admin edit overtime #%s — no managers found, skipping email", request_id)
        else:
            await send_approval_email(request_id, employee, managers)

        audit = AuditTrailBuilder("admin_edit")
        audit.add_step("Admin edit", before, after, f"Reason: {reason}")
        await write_audit_log(settings.SP_LIST_OVERTIME_REQUESTS, request_id, audit)

    from app.services.approval_versions import get_current_version
    new_version = await get_current_version(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    logger.info("Admin edit applied to overtime #%s — version %s, reason: %s", request_id, new_version, reason)
    return {"status": "saved", "new_version": new_version, "fields": after}


async def approve_overtime_request(
    request_id: str | int, manager_id: str | int, channel: str = "system", link_version: int | None = None,
) -> dict:
    """Process overtime approval — update balance, vacation offset, recalc RAD.

    Args:
        request_id: SharePoint item id of the overtime request being approved.
        manager_id: Staff Directory id of whoever is approving.
        channel: Which channel asked (for the Postgres decision path's audit).
        link_version: The approval version a link carried, or None (stale check).

    Returns:
        A result dict describing the approval. An "error" key means nothing was
        written and the request is unchanged.

    Raises:
        SystemPaused: While the storage switch is running (nothing is written).
    """
    await raise_if_paused()                                      # no approval mid-switch
    from app.services.request_decisions import approve as _pg_approve, decisions_active
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_approve("overtime", request_id, manager_id, channel, link_version)

    # Conflict check runs BEFORE the idempotency claim so a blocked approval
    # stays retryable — same reasoning as approve_leave_request.
    pre_claim = await request_store.get_list_item_or_none(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    if pre_claim is None:
        # Deleted between the email going out and the manager acting on it.
        # A missing item is a terminal state, not a transient failure.
        return {"error": "This request no longer exists."}
    conflict = await find_overtime_approval_conflict(request_id, pre_claim["fields"])
    if conflict:
        # Nothing written; the request stays Pending for the manager to decide.
        return {"error": conflict}

    if not await claim_action(settings.SP_LIST_OVERTIME_REQUESTS, request_id, "approve"):
        return {"error": "Already processed"}

    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    # Resolve employee from SubmittedBy Person/Group field
    employee = await resolve_request_submitter(fields, "SubmittedBy")
    if not employee:
        return {"error": "Employee not found"}

    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]
    hours = float(fields.get("Hours", 0) or 0)
    days_to_add = hours / 8

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    # Hourly staff, Ontario and BC (#300): paid, not banked, so simplified
    if not overtime_costs_balance(emp_fields):
        await request_store.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {"Status": "Approved", "ApprovedDate": date.today().isoformat()},
        )

        # Same as the salaried branch below - an approved entry reserves the
        # date whether or not it moves a balance.
        await notify_requests_blocked_by_approval(
            "overtime", request_id, fields, emp_fields.get("EmailAddress", ""),
        )

        from app.templates_render import render_overtime_hourly_approved
        html = render_overtime_hourly_approved(fields, submitter_name, mgr_fields.get("Title", ""))
        await send_email(
            to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
            subject=f"Overtime Approved - Hourly - {submitter_name}",
            html_body=html,
            reply_to=mgr_fields.get("EmailAddress", ""),       # replies reach the manager
        )
        return {"status": "approved", "hourly": True}

    async with lock_manager.lock(employee_id):
        # Calculate new overtime balance
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("approve")
        before = snapshot_balances(ef)

        current_ot = float(ef.get("CurrentOvertimeBalance", 0) or 0)
        new_ot = current_ot + days_to_add

        # Update SD balance
        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentOvertimeBalance": new_ot},
        )
        audit.add_step(
            "Add overtime to Make-Up",
            {"CurrentOvertimeBalance": current_ot},
            {"CurrentOvertimeBalance": new_ot},
            f"Added {days_to_add} days ({hours} hours)",
        )

        # Update overtime request
        await request_store.update_list_item_fields(
            settings.SP_LIST_OVERTIME_REQUESTS, request_id,
            {"Status": "Approved", "ApprovedDate": date.today().isoformat()},
        )

        # This date is now reserved, which may have stranded another entry the
        # employee already had in. Reads only and swallows its own failures.
        await notify_requests_blocked_by_approval(
            "overtime", request_id, fields, emp_fields.get("EmailAddress", ""),
        )

        # Vacation offset logic
        await apply_vacation_offset(employee_id)

        # Recalculate Request Allow Date
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        # Record vacation offset step if balances changed
        post_offset = snapshot_balances(ef)
        if (before["CurrentVacationBalance"] != post_offset["CurrentVacationBalance"]
                or new_ot != post_offset["CurrentOvertimeBalance"]):
            audit.add_step(
                "Vacation offset",
                {"CurrentVacationBalance": before["CurrentVacationBalance"], "CurrentOvertimeBalance": new_ot},
                {"CurrentVacationBalance": post_offset["CurrentVacationBalance"], "CurrentOvertimeBalance": post_offset["CurrentOvertimeBalance"]},
                "Offset negative vacation against positive overtime",
            )

        await recalculate_request_allow_date(
            employee_id,
            float(ef.get("CurrentVacationBalance", 0) or 0),
            float(ef.get("CarryOver", 0) or 0),
        )

    # Send approval email with updated balances
    emp = await get_employee_by_id(employee_id)
    ef = emp["fields"]
    balances = {
        "CurrentVacationBalance": float(ef.get("CurrentVacationBalance", 0) or 0),
        "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": float(ef.get("CarryOver", 0) or 0),
        "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
    }

    from app.templates_render import render_overtime_approved
    html = render_overtime_approved(fields, submitter_name, mgr_fields.get("Title", ""), balances)
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} Overtime Approved - {fields.get('StartDate', '')}",
        html_body=html,
        primary_employee_id=employee_id,
        reply_to=mgr_fields.get("EmailAddress", ""),            # replies reach the manager
    )

    await write_audit_log(settings.SP_LIST_OVERTIME_REQUESTS, request_id, audit)

    return {"status": "approved", "balances": balances}


async def refund_overtime_request(
    request_id: str | int, admin_id: str | int, channel: str = "system",
) -> dict:
    """Reverse an approved overtime request — subtract from OT balance, cascade, recalc RAD."""
    await raise_if_paused()                                      # no refund mid-switch
    from app.services.request_decisions import decisions_active, refund as _pg_refund
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_refund("overtime", request_id, admin_id, channel)
    item = await request_store.get_list_item(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Approved":
        return {"error": "Only approved requests can be refunded"}

    employee = await resolve_request_submitter(fields, "SubmittedBy")
    if not employee:
        return {"error": "Employee not found"}

    emp_fields = employee["fields"]
    submitter_name = emp_fields.get("Title", "")
    employee_id = employee["id"]
    hours = float(fields.get("Hours", 0) or 0)
    days_to_subtract = hours / 8

    # Update SP status
    await request_store.update_list_item_fields(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, {"Status": "Refunded"},
    )

    # Hourly staff (Ontario and BC, #300): nothing was banked, so nothing comes
    # back. A BC approval from before #300 was run as salaried and has an
    # audit log; that one falls through and is reversed like a salaried one.
    banked = extract_approval_deltas(fields.get("BalanceAuditLog", "") or "") is not None
    if not overtime_costs_balance(emp_fields) and not banked:
        from app.templates_render import render_refund_notification
        html = render_refund_notification("Overtime", request_id, submitter_name, fields, None)
        await send_email_with_dashboard(
            to=[emp_fields.get("EmailAddress", "")],
            subject=f"{submitter_name} - Overtime Request: Refunded",
            html_body=html,
            primary_employee_id=employee_id,
        )
        return {"status": "refunded", "no_balance_change": True}

    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]

        audit = AuditTrailBuilder("refund")
        before = snapshot_balances(ef)

        current_ot = float(ef.get("CurrentOvertimeBalance", 0) or 0)
        new_ot = current_ot - days_to_subtract

        await sp_client.update_list_item_fields(
            settings.SP_LIST_STAFF_DIRECTORY, employee_id,
            {"CurrentOvertimeBalance": new_ot},
        )
        audit.add_step(
            "Refund overtime from Make-Up",
            {"CurrentOvertimeBalance": current_ot},
            {"CurrentOvertimeBalance": new_ot},
            f"Subtracted {days_to_subtract} days ({hours} hours)",
        )

        pre_cascade = {
            "CurrentSickDayBalance": before["CurrentSickDayBalance"],
            "CurrentOvertimeBalance": new_ot,
            "CarryOver": before["CarryOver"],
            "CurrentVacationBalance": before["CurrentVacationBalance"],
        }

        balances = await cascade_current_year(employee_id)

        cascade_after = {k: balances[k] for k in pre_cascade if k in balances}
        audit.add_step(
            "Cascade (current year)", pre_cascade, cascade_after,
            describe_cascade_changes(pre_cascade, cascade_after),
        )

        await recalculate_request_allow_date(
            employee_id, balances["CurrentVacationBalance"], balances["CarryOver"]
        )

    await write_audit_log(settings.SP_LIST_OVERTIME_REQUESTS, request_id, audit)

    from app.templates_render import render_refund_notification
    html = render_refund_notification("Overtime", request_id, submitter_name, fields, balances)
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} - Overtime Request: Refunded",
        html_body=html,
        primary_employee_id=employee_id,
    )

    return {"status": "refunded", "balances": balances}


async def reject_overtime_request(
    request_id: str | int, manager_id: str | int, channel: str = "system",
    link_version: int | None = None, reason: str | None = None,
) -> dict:
    """Process overtime rejection."""
    await raise_if_paused()                                      # no rejection mid-switch
    from app.services.request_decisions import decisions_active, reject as _pg_reject
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_reject("overtime", request_id, manager_id, channel, link_version, reason)
    if not await claim_action(settings.SP_LIST_OVERTIME_REQUESTS, request_id, "reject"):
        return {"error": "Already processed"}

    # Read without raising: a link may reject a request that was since deleted,
    # which must read as a clean message rather than a 500 (#294 S6).
    item = await request_store.get_list_item_or_none(settings.SP_LIST_OVERTIME_REQUESTS, request_id)
    if item is None:
        return {"error": "This request no longer exists."}
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": "Not pending"}

    employee = await resolve_request_submitter(fields, "SubmittedBy")
    submitter_name = employee["fields"].get("Title", "") if employee else ""
    emp_fields = employee["fields"] if employee else {}

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    await request_store.update_list_item_fields(
        settings.SP_LIST_OVERTIME_REQUESTS, request_id, {"Status": "Rejected"}
    )

    # Read balances for rejection email
    balances = {}
    if employee:
        employee_id = employee["id"]
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]
        balances = {
            "CurrentVacationBalance": float(ef.get("CurrentVacationBalance", 0) or 0),
            "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
            "CarryOver": float(ef.get("CarryOver", 0) or 0),
            "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
        }

    from app.templates_render import render_overtime_rejected
    html = render_overtime_rejected(fields, submitter_name, mgr_fields.get("Title", ""), balances)
    emp_id = employee["id"] if employee else None
    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", ""), mgr_fields.get("EmailAddress", "")],
        subject=f"{submitter_name} Overtime Rejected - {fields.get('StartDate', '')}",
        html_body=html,
        primary_employee_id=emp_id,
        reply_to=mgr_fields.get("EmailAddress", ""),            # replies reach the manager
    )

    return {"status": "rejected"}
