import logging
from datetime import date

from app.config import settings
from app.graph.email import send_email, send_email_with_dashboard
from app.services.sms import send_sms
from app.services.employee import (
    get_employee_by_email,
    get_employee_by_id,
    get_manager_for_employee,
    get_all_managers_for_employee,
)
from app.services.balance import recalculate_request_allow_date
from app.services.employment import is_hourly
from app.services.concurrency import lock_manager
from app.services.idempotency import approval_email_action, claim_action, release_claim
from app.services.approval_links import generate_approval_url
from app.services.approval_versions import (
    bump_and_snapshot,
    get_current_version,
    get_previous_snapshot,
    MATERIAL_FIELDS_CARRYOVER_PAYOUT,
)
from app.services.leave_requests import _resolve_user_lookup_id
from app.services.request_submitter import UnknownSubmitter, submitter_columns
from app.services.audit_trail import (
    AuditTrailBuilder,
    write_audit_log,
)
from app.repositories.request_store import request_store, requests_in_postgres
from app.services.switch_pause import raise_if_paused  # refuse writes while the storage switch runs
from app.repositories.staff_store import staff_store  # Staff Directory reads/writes follow staff_storage

logger = logging.getLogger(__name__)

# processing_log action marking the employee's "Request Received" email as sent.
RECEIVED_EMAIL_ACTION = "request_received_email"


async def process_new_carryover_payout(
    form_data: dict, submitter_email: str, source: str | None = None,
    notify: bool = True,
) -> dict:
    """Create the SharePoint carryover/payout item, assign its manager, validate.

    Args:
        form_data: The form (type_of_request "Carry Over" or "Payout", days).
        submitter_email: Who the request is for.
        source: Where it came from (a request_submitter.SOURCE_* value),
            written to RequestSource when those columns are enabled.
        notify: True runs the approval pipeline (asks the manager), as a normal
            submission does. False assigns the manager and its ids but skips
            that pipeline, so an admin can create and approve it at once (#283).

    Returns:
        The created SharePoint item.

    Raises:
        SystemPaused: While the storage switch is running (no request is created).
    """
    await raise_if_paused()                                      # no new request mid-switch
    request_type = form_data.get("type_of_request", "")  # "Carry Over" or "Payout"
    days = float(form_data.get("days", 0))

    fields = {
        "TypeofRequest": request_type,
        "Days": days,
        "SystemState": "Not Processed",
    }

    lookup_id = await _resolve_user_lookup_id(submitter_email)
    if lookup_id:
        fields["SubmittedByLookupId"] = lookup_id
    # SubmitterEmail and RequestSource (carryover already matches by email below).
    fields.update(submitter_columns(submitter_email, source))

    # Once requests live in Postgres the typed row needs the submitter's staff id
    # (employee_id is not null), so resolve by email now and refuse a request that
    # matches nobody rather than writing a half-formed row (#324). SharePoint mode
    # is unchanged: there EmployeeID is written later by auto_assign_manager.
    if await requests_in_postgres():
        submitter = await get_employee_by_email(submitter_email)
        if not submitter:
            raise UnknownSubmitter(
                f"No staff record was found for {submitter_email}, so the request was not created."
            )
        fields["EmployeeID"] = str(submitter["id"])             # stamps employee_id on the typed row

    item = await request_store.create_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, fields)
    item_id = item["id"]
    logger.info("Created carryover/payout request #%s", item_id)

    await auto_assign_manager(item_id, submitter_email, notify=notify)   # notify=False skips the approval pipeline
    return item


async def auto_assign_manager(request_id: str | int, submitter_email: str, notify: bool = True):
    """Look up employee by email → supervisor → assign manager + IDs.

    Args:
        request_id: The carry-over/payout item.
        submitter_email: Who the request is for.
        notify: True runs the approval pipeline at the end; False stops after
            the manager and ids are set, for an admin-created-and-approved one.
    """
    # Once requests and staff both live in Postgres, the submission module records
    # the request and QUEUES the manager email, text and the receipt, instead of
    # running the SharePoint approval pipeline and sending here (#324, #326). The
    # payout-cap and hourly auto-rejections the pipeline does at submit are not
    # replayed here; a payout past the vacation floor is still refused at approval.
    from app.services.request_submission import submission_active, submit
    if await submission_active():
        await submit("carryover_payout", request_id, notify=notify)
        return

    employee = await get_employee_by_email(submitter_email)
    if not employee:
        logger.warning("Cannot assign manager — employee not found for CO/PO #%s", request_id)
        return

    employee_id = employee["id"]

    manager = await get_manager_for_employee(employee)
    if not manager:
        return

    mgr_fields = manager["fields"]
    manager_id = manager["id"]
    mgr_email = mgr_fields.get("EmailAddress", "")
    manager_lookup_id = await _resolve_user_lookup_id(mgr_email)

    update = {
        "SystemState": "Not Processed",
        "EmployeeID": int(employee_id),
        "ManagerID": int(manager_id),
    }
    if manager_lookup_id:
        update["ManagerLookupId"] = manager_lookup_id
    # No AllManagers here: unlike the leave list, the carry-over/payout list has
    # no such column, and SharePoint refuses a whole update that names a column
    # the list lacks, so ManagerLookupId would never land (#199).

    await request_store.update_list_item_fields(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, update)
    logger.info("Assigned manager %s to CO/PO request #%s", mgr_fields.get("Title"), request_id)

    # Trigger approval pipeline, unless an admin is approving it themselves.
    if notify:
        await run_approval_pipeline(request_id)
    else:
        # Admin create-and-approve: suppress the whole pipeline now and durably,
        # so the dispatcher's later change-notice call sends nothing (#294 B1).
        await _suppress_approval_notification(request_id)


async def _suppress_approval_notification(request_id: str | int) -> None:
    """Mark the approval pipeline handled without sending any of its emails.

    run_approval_pipeline sends the employee a "Request Received" email (guarded
    by RECEIVED_EMAIL_ACTION) and then the manager approval email (guarded by
    the per-version claim). This claims both without sending, and moves
    SystemState off "Not Processed", so when the dispatcher runs the pipeline for
    this item it has nothing left to send. Undone by
    notify_manager_after_failed_admin_approval if the admin's approval fails.

    Args:
        request_id: The carry-over/payout item.
    """
    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]                                     # TypeofRequest, Days: the version snapshot
    version = await bump_and_snapshot(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, fields, MATERIAL_FIELDS_CARRYOVER_PAYOUT,
    )                                                           # same version send_approval_email would use
    await claim_action(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, approval_email_action(version),
    )                                                           # the manager approval email
    await claim_action(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, RECEIVED_EMAIL_ACTION,
    )                                                           # the employee "Request Received" email
    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, {"SystemState": "Processing"},
    )                                                           # off "Not Processed": the pipeline won't restart


async def notify_manager_after_failed_admin_approval(request_id: str | int) -> bool:
    """Undo the suppressed pipeline and ask the manager, after an admin's
    create-and-approve did not approve the request (#294 S3).

    Turns an admin-created carry-over/payout the admin could not approve into an
    ordinary Pending one that runs the normal pipeline (received email, then the
    manager approval email). Does nothing when the request is no longer Pending
    or has been processed, so a terminal decision is not reopened.

    Args:
        request_id: The carry-over/payout item.

    Returns:
        True when the pipeline was run; False when it was not in a state to run.
    """
    item = await request_store.get_list_item_or_none(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    if item is None:
        return False
    fields = item["fields"]
    if fields.get("Status") != "Pending" or fields.get("SystemState") == "Processed":
        return False                                            # gone or already decided: leave it
    version = await get_current_version(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    await release_claim(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, approval_email_action(version),
    )                                                           # give back the manager-email claim
    await release_claim(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, RECEIVED_EMAIL_ACTION,
    )                                                           # give back the received-email claim
    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, {"SystemState": "Not Processed"},
    )                                                           # back to where the pipeline starts
    await run_approval_pipeline(request_id)                     # ask the manager the normal way
    return True


async def run_approval_pipeline(request_id: str | int):
    """Pre-validate → send confirmation → send approval email."""
    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if not fields.get("ManagerLookupId"):
        return
    if fields.get("SystemState") != "Not Processed":
        return

    # Set SystemState to Processing
    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, {"SystemState": "Processing"}
    )

    employee_id = fields.get("EmployeeID")
    manager_id = fields.get("ManagerID")
    if not employee_id or not manager_id:
        return

    employee = await get_employee_by_id(employee_id)
    manager = await get_employee_by_id(manager_id)
    if not employee or not manager:
        return

    emp_fields = employee["fields"]
    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)

    new_vacation = current_vacation - days

    # Type-specific validation
    if request_type == "Payout":
        new_payout = current_payout + days
        if new_payout > 5:
            # Payout cap auto-reject
            from app.services.auto_reject_titles import append_auto_reject_tag
            reason = f"Payout cap exceeded — new total would be {new_payout} days (max 5)."
            await request_store.update_list_item_fields(
                settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
                {
                    "Title": append_auto_reject_tag(fields.get("Title", ""), reason),
                    "Status": "Rejected",
                    "SystemState": "Processed",
                },
            )
            from app.templates_render import render_payout_cap_rejected
            html = render_payout_cap_rejected(request_id, emp_fields, reason)
            await send_email(
                to=[emp_fields.get("EmailAddress", "")],
                subject="Payout Request - Auto Rejected",
                html_body=html,
            )
            logger.info("Auto-rejected CO/PO #%s — payout cap exceeded", request_id)
            return
        new_carryover = current_carryover
    else:
        new_carryover = current_carryover + days
        new_payout = current_payout

    # Vacation cannot go negative. Hourly staff have no vacation balance at all
    # (theirs is only a counter of days taken, #300), so theirs is refused too.
    hourly = is_hourly(emp_fields)
    if hourly or new_vacation < 0:
        from app.services.auto_reject_titles import append_auto_reject_tag
        reason = (
            "Hourly staff have no vacation balance to carry over or pay out."
            if hourly else
            f"Vacation balance would go to {new_vacation} days "
            f"({current_vacation} - {days})."
        )
        await request_store.update_list_item_fields(
            settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
            {
                "Title": append_auto_reject_tag(fields.get("Title", ""), reason),
                "Status": "Rejected",
                "SystemState": "Processed",
            },
        )
        from app.templates_render import render_system_override_reject
        html = render_system_override_reject(
            request_id, emp_fields, request_type,
            current_vacation, current_carryover, current_payout,
            reason,
        )
        await send_email(
            to=[emp_fields.get("EmailAddress", "")],
            subject="Carry Over / Payout Request - Auto Rejected",
            html_body=html,
        )
        logger.info("Auto-rejected CO/PO #%s - %s", request_id, "hourly" if hourly else "vacation would go negative")
        return

    # Send confirmation to employee, once per request: the request page runs this
    # pipeline, then SharePoint's change notice for the same new item runs it again.
    if await claim_action(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, RECEIVED_EMAIL_ACTION):
        try:
            from app.templates_render import render_carryover_confirmation, render_payout_confirmation
            if request_type == "Carry Over":
                html = render_carryover_confirmation(
                    request_id, emp_fields, days, new_vacation, new_carryover, current_payout
                )
                await send_email(
                    to=[emp_fields.get("EmailAddress", "")],
                    subject="Request Received for Carry Over",
                    html_body=html,
                )
            else:
                html = render_payout_confirmation(
                    request_id, emp_fields, days, new_vacation, current_carryover, new_payout
                )
                await send_email(
                    to=[emp_fields.get("EmailAddress", "")],
                    subject="Request Received for Payout",
                    html_body=html,
                )
        except BaseException:                                   # not sent: let a later run retry
            try:
                await release_claim(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, RECEIVED_EMAIL_ACTION)
            except Exception:  # noqa: BLE001 - keep the send error
                logger.exception("Could not release the confirmation claim for CO/PO #%s", request_id)
            raise

    await send_approval_email(request_id)


async def send_approval_email(request_id: str | int, is_reminder: bool = False):
    """Send the manager approval email(s) for an in-flight CO/PO request.

    Reads fresh SP + employee state so it can be called standalone (e.g. from
    the admin edit endpoint after fields have been updated).
    """
    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return
    if fields.get("SystemState") == "Processed":
        return

    employee_id = fields.get("EmployeeID")
    if not employee_id:
        return

    employee = await get_employee_by_id(employee_id)
    if not employee:
        return

    emp_fields = employee["fields"]
    employee_name = emp_fields.get("Title", "")
    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    current_vacation = float(emp_fields.get("CurrentVacationBalance", 0) or 0)
    current_carryover = float(emp_fields.get("CarryOver", 0) or 0)
    current_payout = float(emp_fields.get("Payout", 0) or 0)

    new_vacation = current_vacation - days
    if request_type == "Carry Over":
        new_carryover = current_carryover + days
        new_payout = current_payout
    else:
        new_carryover = current_carryover
        new_payout = current_payout + days

    all_managers = await get_all_managers_for_employee(employee)
    if not all_managers:
        manager_id_field = fields.get("ManagerID")
        if manager_id_field:
            mgr = await get_employee_by_id(manager_id_field)
            if mgr:
                all_managers = [mgr]
    if not all_managers:
        return

    version = await bump_and_snapshot(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, fields, MATERIAL_FIELDS_CARRYOVER_PAYOUT,
        force_bump=is_reminder,
    )
    previous_snapshot = (
        await get_previous_snapshot(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
        if version > 1 and not is_reminder else None
    )

    from app.templates_render import render_carryover_payout_approval_email

    # One approval email per version. The request page sends it as it creates
    # the request; SharePoint's change notice for that same new item then reaches
    # the dispatcher seconds later and asks again. Whoever claims the version first
    # sends; a later call for the same version stops here. A real change or a
    # reminder makes a new version, so those still send.
    sent_key = approval_email_action(version)
    if not await claim_action(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, sent_key):
        logger.info("Approval email for carry-over/payout request #%s version %s already sent; not sending again", request_id, version)
        return
    try:
        for mgr in all_managers:
            mgr_id = mgr["id"]
            mgr_email = mgr["fields"].get("EmailAddress", "")

            approve_url = generate_approval_url("carryover-payout", request_id, "approve", mgr_id, approval_version=version)
            reject_url = generate_approval_url("carryover-payout", request_id, "reject", mgr_id, approval_version=version)

            html = render_carryover_payout_approval_email(
                request_id, request_type, employee_name, days,
                current_vacation, current_carryover, current_payout,
                new_vacation, new_carryover, new_payout,
                approve_url, reject_url,
                previous_snapshot=previous_snapshot,
            )
            subject = ("Reminder: " if is_reminder else "") + f"{request_type} Request #{request_id} Submitted by {employee_name}"
            await send_email_with_dashboard(
                to=[mgr_email, "mandyl@ucsh.com"],
                subject=subject,
                html_body=html,
                primary_employee_id=mgr_id,
                reply_to=emp_fields.get("EmailAddress", ""),       # replies reach the employee
            )

            cell = mgr["fields"].get("CellNumber", "")
            if cell and not is_reminder:
                # A failed text must not undo the emails: raising here would release
                # the claim and let the next caller email this manager again.
                try:
                    await send_sms(
                        to=cell,
                        body=(
                            f"{request_type} Request #{request_id} for {employee_name} ({days} days).\n"
                            f"If approved: Vac: {new_vacation}, CO: {new_carryover}, PO: {new_payout}.\n"
                            f"Reply \"CO Approve {request_id}\" or \"CO Reject {request_id}\""
                        ),
                    )
                except Exception:  # noqa: BLE001 - logged; the email already went out
                    logger.exception("Could not text the manager about carry-over/payout request #%s", request_id)

        logger.info("Sent approval email for CO/PO #%s to %d manager(s)", request_id, len(all_managers))
    except BaseException:                                       # cancellation too (a redeploy mid-send)
        # The send failed: drop the claim so the next change notice or reminder can retry.
        try:
            await release_claim(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, sent_key)
        except Exception:  # noqa: BLE001 - keep the send error, which is the one that matters
            logger.exception("Could not release the approval email claim for #%s", request_id)
        raise


ALLOWED_CARRYOVER_TYPES = {"Carry Over", "Payout"}


async def admin_edit_carryover_payout(
    request_id: str | int,
    payload: dict,
    reason: str,
) -> dict:
    """Apply an admin-driven edit to a pending CO/PO request, re-send approval email."""
    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Pending":
        return {"error": f"Request is not Pending (current status: {fields.get('Status')})"}
    if fields.get("SystemState") == "Processed":
        return {"error": "Request has already been processed"}

    new_type = payload.get("TypeofRequest")
    if new_type not in ALLOWED_CARRYOVER_TYPES:
        return {"error": f"Invalid TypeofRequest: {new_type}"}

    try:
        new_days = float(payload["Days"])
    except (KeyError, ValueError, TypeError):
        return {"error": "Days must be a number"}
    if new_days <= 0:
        return {"error": "Days must be > 0"}

    employee_id = fields.get("EmployeeID")
    if not employee_id:
        return {"error": "Request has no EmployeeID — cannot edit"}

    before = {
        "TypeofRequest": fields.get("TypeofRequest"),
        "Days": fields.get("Days"),
    }
    after = {
        "TypeofRequest": new_type,
        "Days": new_days,
    }

    # Once requests and staff live in Postgres the edit is applied in one
    # transaction: bump the approval version (old links go stale), record the field
    # changes, append an "edited" event and queue fresh manager links through the
    # outbox (#324). SharePoint mode keeps the send path below.
    from app.services.request_submission import apply_admin_edit, submission_active
    if await submission_active():
        return await apply_admin_edit("carryover_payout", request_id, before, after, reason)

    async with lock_manager.lock(employee_id):
        fresh = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
        ff = fresh["fields"]
        if ff.get("Status") != "Pending":
            return {"error": "Request status changed during edit — refresh and try again"}
        if ff.get("SystemState") == "Processed":
            return {"error": "Request was just processed — refresh and try again"}

        await request_store.update_list_item_fields(
            settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
            {
                "TypeofRequest": new_type,
                "Days": new_days,
            },
        )

        await send_approval_email(request_id)

        audit = AuditTrailBuilder("admin_edit")
        audit.add_step("Admin edit", before, after, f"Reason: {reason}")
        await write_audit_log(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, audit)

    from app.services.approval_versions import get_current_version
    new_version = await get_current_version(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    logger.info("Admin edit applied to CO/PO #%s — version %s, reason: %s", request_id, new_version, reason)
    return {"status": "saved", "new_version": new_version, "fields": after}


async def approve_carryover_payout(
    request_id: str | int, manager_id: str | int, channel: str = "system", link_version: int | None = None,
) -> dict:
    """Process approval — re-validate, apply balance transfer, recalc RAD."""
    await raise_if_paused()                                      # no approval mid-switch
    from app.services.request_decisions import approve as _pg_approve, decisions_active
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_approve("carryover_payout", request_id, manager_id, channel, link_version)
    if not await claim_action(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, "approve"):
        return {"error": "Already processed"}

    # Read without raising: a link may approve a request that was since deleted,
    # which must read as a clean message rather than a 500 (#294 S6).
    item = await request_store.get_list_item_or_none(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    if item is None:
        return {"error": "This request no longer exists."}
    fields = item["fields"]

    if fields.get("SystemState") == "Processed":
        return {"error": "Already processed"}

    employee_id = fields.get("EmployeeID")
    if not employee_id:
        return {"error": "No employee ID"}

    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    async with lock_manager.lock(employee_id):
        # Re-read fresh balances
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]
        fresh_vacation = float(ef.get("CurrentVacationBalance", 0) or 0)
        fresh_carryover = float(ef.get("CarryOver", 0) or 0)
        fresh_payout = float(ef.get("Payout", 0) or 0)

        final_vacation = fresh_vacation - days

        # Re-validate at approval time
        if final_vacation < 0:
            from app.services.auto_reject_titles import append_auto_reject_tag
            reason = (
                f"Vacation balance has dropped since submission — would go to "
                f"{final_vacation} days ({fresh_vacation} - {days})."
            )
            # System override reject
            await request_store.update_list_item_fields(
                settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
                {
                    "Title": append_auto_reject_tag(fields.get("Title", ""), reason),
                    "Status": "Rejected",
                    "SystemState": "Processed",
                },
            )
            employee_name = ef.get("Title", "")
            from app.templates_render import render_system_override_reject_at_approval
            html = render_system_override_reject_at_approval(
                request_id, employee_name, request_type, reason,
            )
            recipients = [ef.get("EmailAddress", "")]
            if mgr_fields.get("EmailAddress"):
                recipients.append(mgr_fields["EmailAddress"])
            recipients.append("mandyl@ucsh.com")
            await send_email(
                to=recipients,
                subject=f"System Override Reject: {request_type} Request #{request_id} Submitted by {employee_name}",
                html_body=html,
            )
            return {"status": "system_override_rejected"}

        audit = AuditTrailBuilder("approve")

        # Apply balance transfer
        if request_type == "Carry Over":
            final_carryover = fresh_carryover + days
            final_payout = fresh_payout
            await staff_store.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "CarryOver": final_carryover},
            )
            audit.add_step(
                "Transfer Vacation to Carry Over",
                {"CurrentVacationBalance": fresh_vacation, "CarryOver": fresh_carryover},
                {"CurrentVacationBalance": final_vacation, "CarryOver": final_carryover},
                f"Transferred {days} days",
            )
        else:
            final_carryover = fresh_carryover
            final_payout = fresh_payout + days
            await staff_store.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "Payout": final_payout},
            )
            audit.add_step(
                "Transfer Vacation to Payout",
                {"CurrentVacationBalance": fresh_vacation, "Payout": fresh_payout},
                {"CurrentVacationBalance": final_vacation, "Payout": final_payout},
                f"Transferred {days} days",
            )

        # Recalculate Request Allow Date
        await recalculate_request_allow_date(employee_id, final_vacation, final_carryover)

    await write_audit_log(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, audit)

    # Update request
    new_balance_str = f"{{Vacation:{final_vacation}, CarryOver:{final_carryover}, Payout:{final_payout}}}"
    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
        {"Status": "Approved", "SystemState": "Processed", "NewBalance": new_balance_str, "ApprovedDate": date.today().isoformat()},
    )

    # Send approval email
    emp = await get_employee_by_id(employee_id)
    ef = emp["fields"]
    from app.templates_render import render_carryover_approved, render_payout_approved
    employee_name = ef.get("Title", "")
    balances = {
        "CurrentVacationBalance": final_vacation,
        "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": final_carryover,
        "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
        "Payout": final_payout,
    }

    if request_type == "Carry Over":
        html = render_carryover_approved(request_id, employee_name, balances)
        subject = f"Carry Over Request #{request_id} Approved"
    else:
        html = render_payout_approved(request_id, employee_name, balances)
        subject = f"Payout Request #{request_id} Approved"

    await send_email_with_dashboard(
        to=[ef.get("EmailAddress", "")],
        cc=[mgr_fields.get("EmailAddress", "")],
        subject=subject,
        html_body=html,
        importance="High",
        primary_employee_id=employee_id,
        reply_to=mgr_fields.get("EmailAddress", ""),            # replies reach the manager
    )

    return {"status": "approved", "balances": balances}


async def refund_carryover_payout(
    request_id: str | int, admin_id: str | int, channel: str = "system",
) -> dict:
    """Reverse an approved carryover/payout — restore vacation, subtract from CO/Payout, recalc RAD."""
    await raise_if_paused()                                      # no refund mid-switch
    from app.services.request_decisions import decisions_active, refund as _pg_refund
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_refund("carryover_payout", request_id, admin_id, channel)
    item = await request_store.get_list_item(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    fields = item["fields"]

    if fields.get("Status") != "Approved" or fields.get("SystemState") != "Processed":
        return {"error": "Only approved/processed requests can be refunded"}

    employee_id = fields.get("EmployeeID")
    if not employee_id:
        return {"error": "No employee ID"}

    days = float(fields.get("Days", 0) or 0)
    request_type = fields.get("TypeofRequest", "")

    async with lock_manager.lock(employee_id):
        emp = await get_employee_by_id(employee_id)
        ef = emp["fields"]
        fresh_vacation = float(ef.get("CurrentVacationBalance", 0) or 0)
        fresh_carryover = float(ef.get("CarryOver", 0) or 0)
        fresh_payout = float(ef.get("Payout", 0) or 0)

        final_vacation = fresh_vacation + days

        audit = AuditTrailBuilder("refund")

        if request_type == "Carry Over":
            final_carryover = fresh_carryover - days
            final_payout = fresh_payout
            await staff_store.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "CarryOver": final_carryover},
            )
            audit.add_step(
                "Reverse Carry Over to Vacation",
                {"CurrentVacationBalance": fresh_vacation, "CarryOver": fresh_carryover},
                {"CurrentVacationBalance": final_vacation, "CarryOver": final_carryover},
                f"Reversed {days} days",
            )
        else:
            final_carryover = fresh_carryover
            final_payout = fresh_payout - days
            await staff_store.update_list_item_fields(
                settings.SP_LIST_STAFF_DIRECTORY, employee_id,
                {"CurrentVacationBalance": final_vacation, "Payout": final_payout},
            )
            audit.add_step(
                "Reverse Payout to Vacation",
                {"CurrentVacationBalance": fresh_vacation, "Payout": fresh_payout},
                {"CurrentVacationBalance": final_vacation, "Payout": final_payout},
                f"Reversed {days} days",
            )

        await recalculate_request_allow_date(employee_id, final_vacation, final_carryover)

    await write_audit_log(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, audit)

    # Update SP status
    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id, {"Status": "Refunded"},
    )

    emp = await get_employee_by_id(employee_id)
    ef = emp["fields"]
    employee_name = ef.get("Title", "")
    balances = {
        "CurrentVacationBalance": final_vacation,
        "CurrentSickDayBalance": float(ef.get("CurrentSickDayBalance", 0) or 0),
        "CarryOver": final_carryover,
        "CurrentOvertimeBalance": float(ef.get("CurrentOvertimeBalance", 0) or 0),
        "Payout": final_payout,
    }

    from app.templates_render import render_refund_notification
    html = render_refund_notification(request_type, request_id, employee_name, fields, balances)
    await send_email_with_dashboard(
        to=[ef.get("EmailAddress", "")],
        subject=f"{employee_name} - {request_type} Request: Refunded",
        html_body=html,
        primary_employee_id=employee_id,
    )

    return {"status": "refunded", "balances": balances}


async def reject_carryover_payout(
    request_id: str | int, manager_id: str | int, channel: str = "system",
    link_version: int | None = None, reason: str | None = None,
) -> dict:
    """Process rejection."""
    await raise_if_paused()                                      # no rejection mid-switch
    from app.services.request_decisions import decisions_active, reject as _pg_reject
    if await decisions_active():                                 # one decision path once both are in Postgres
        return await _pg_reject("carryover_payout", request_id, manager_id, channel, link_version, reason)
    if not await claim_action(settings.SP_LIST_CARRYOVER_PAYOUT, request_id, "reject"):
        return {"error": "Already processed"}

    # Read without raising: a link may reject a request that was since deleted,
    # which must read as a clean message rather than a 500 (#294 S6).
    item = await request_store.get_list_item_or_none(settings.SP_LIST_CARRYOVER_PAYOUT, request_id)
    if item is None:
        return {"error": "This request no longer exists."}
    fields = item["fields"]

    if fields.get("SystemState") == "Processed":
        return {"error": "Already processed"}

    employee_id = fields.get("EmployeeID")
    request_type = fields.get("TypeofRequest", "")

    await request_store.update_list_item_fields(
        settings.SP_LIST_CARRYOVER_PAYOUT, request_id,
        {"Status": "Rejected", "SystemState": "Processed"},
    )

    employee = await get_employee_by_id(employee_id) if employee_id else None
    emp_fields = employee["fields"] if employee else {}
    manager = await get_employee_by_id(manager_id)
    mgr_fields = manager["fields"] if manager else {}

    from app.templates_render import render_carryover_rejected, render_payout_rejected
    if request_type == "Carry Over":
        html = render_carryover_rejected(request_id, fields)
        subject = f"Carry Over Request #{request_id} Rejected"
    else:
        html = render_payout_rejected(request_id, fields)
        subject = f"Payout Request #{request_id} Rejected"

    await send_email_with_dashboard(
        to=[emp_fields.get("EmailAddress", "")],
        cc=[mgr_fields.get("EmailAddress", "")],
        subject=subject,
        html_body=html,
        primary_employee_id=employee_id,
        reply_to=mgr_fields.get("EmailAddress", ""),            # replies reach the manager
    )

    return {"status": "rejected"}
