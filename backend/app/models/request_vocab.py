"""The allowed values for every typed request column, in one place (#321-#341).

Both the database CHECK constraints (in the models and Alembic 0019) and the
services that write these tables import their vocabularies from here, so a
value can never be allowed by the code but refused by the table, or the other
way round. Each tuple is the closed set for one column; ``in_clause`` turns a
tuple into the SQL fragment a CHECK constraint uses, so the two always agree.
"""

# The three request kinds. These are the wire values stored in requests.kind
# and request_events.kind; the URL segment "carryover-payout" is unrelated.
KINDS = ("leave", "overtime", "carryover_payout")

# A request's lifecycle status, stored lower case. The SharePoint adapter shows
# it title case on the wire (Pending, Approved, ...).
STATUSES = ("pending", "approved", "rejected", "cancelled", "refunded")

# A request's position in the explicit state machine (requests.state), stored
# lower snake case (#455). The backend owns this; the frontend only renders it.
# "status" above is kept untouched for compatibility; this is the authoritative
# lifecycle position. Only "waiting_on_approval" can be decided.
STATES_MACHINE = (
    "getting_ready",        # row saved, the backend is still working out days, manager and checks
    "waiting_on_approval",  # submit finished, manager asked (or none found but the request is complete)
    "approved",             # as today's status
    "rejected",             # as today's status
    "refunded",             # as today's status
    "cancelled",            # as today's status
    "needs_attention",      # getting ready failed, or a pending row the old pipeline never finished
)
# A short alias used by the state-machine module and the model CHECK.
STATES = STATES_MACHINE

# Every event that can land on a request's timeline (request_events.type).
EVENT_TYPES = (
    "submitted",         # the request was created
    "sent_to_manager",   # an approval ask went to the manager
    "receipt_sent",      # the employee got a "we received it" note
    "link_opened",       # the manager opened an approval link
    "approved",          # a manager approved
    "rejected",          # a manager rejected
    "refused",           # an action was turned down (stale link, already decided, wrong role)
    "balance_applied",   # balance pots were spent or credited
    "counted",           # hourly vacation added to the vacation-taken counter
    "no_balance_used",   # bereavement, jury duty or hourly overtime: no pot moved
    "result_sent",       # the decision was sent back to the employee
    "send_failed",       # a message could not be sent
    "reminder_sent",     # a reminder went to the manager
    "refunded",          # an approved request was reversed
    "balance_reversed",  # the pots a refund put back
    "edited",            # an admin changed a field
    "cancelled",         # the request was cancelled
    "imported",          # copied in from SharePoint
    "flagged",           # a legacy or SharePoint finding about this request
)

# Where an event, ledger row or change came from (the channel).
CHANNELS = (
    "request_page",       # the in-house public request page
    "form",               # the Microsoft Form feed
    "email_link",         # an approval email link
    "sms",                # a Twilio reply
    "employee_dashboard",
    "manager_dashboard",
    "admin_dashboard",
    "system",             # a background task or automatic step
    "import",             # the SharePoint importer
    "sharepoint_check",   # the post-switch SharePoint sweep
)

# The balance pots a ledger row can move. vacation_taken is the hourly counter,
# not a spendable pot.
POTS = ("sick", "make_up", "carry_over", "vacation", "payout", "vacation_taken")

# How far a request's balance effect has been carried out (requests.balance_state).
BALANCE_STATES = ("not_applied", "applied", "counted", "no_balance", "reversed", "legacy_unknown")

# The leave types the app offers today. Work from Home is offered again on the
# request page (#388); it costs no balance for anyone. It stays last so the
# generated CHECK text is unchanged from when it was the trailing legacy value.
LEAVE_TYPES = (
    "Vacation",
    "Sick or Personal Day",
    "Half Day or Partial Day Off",
    "Bereavement",
    "Jury Duty",
    "Work from Home",
)
# The full set a leave row's leave_type is allowed to be (now exactly LEAVE_TYPES).
LEAVE_TYPES_ALLOWED = LEAVE_TYPES

# How the person who took the leave was paid when it was decided; null for legacy.
EMPLOYMENT_TYPES = ("Salary", "Hourly", "Hourly BC")

# A carry-over/payout request's choice, stored snake case. The adapter maps the
# SharePoint "Carry Over" / "Payout" values to and from these.
REQUEST_OPTIONS = ("carry_over", "payout")

# An outbox message's delivery state. dropped is rehearsal mode skipping an
# address that is not on the allowlist.
OUTBOX_STATUSES = ("pending", "sending", "sent", "failed", "dropped")

# The channel an outbox message is sent over.
OUTBOX_CHANNELS = ("email", "sms")

# The kinds of outbox message, for reference; the column is open, so not CHECKed.
# bereavement_alert is the HR side-notice for a bereavement/jury-duty leave; it
# records no stage-changing event when delivered (see outbox_sender) (finding 3).
# cancel_notice tells the manager and the person a request was cancelled (#519).
OUTBOX_MESSAGE_TYPES = (
    "manager_approval", "receipt", "result", "reminder", "refund", "rejection", "welcome",
    "bereavement_alert", "cancel_notice",
)

# What the SharePoint sweep decided about one item (sharepoint_seen.outcome).
SEEN_OUTCOMES = ("imported", "flagged_new", "flagged_edit", "flagged_delete", "unchanged")

# The jobs that write a sync_runs row. "sharepoint_check_item" is the per-item
# webhook check, kept apart from the scheduled "sharepoint_check" sweep so a
# webhook cannot postpone the scheduled sweeps (#336 fix).
SYNC_JOBS = ("import", "shadow_sync", "final_sync", "sharepoint_check", "sharepoint_check_item")

# What a legacy finding is about (legacy_findings.subject).
FINDING_SUBJECTS = ("request", "staff")

# The named reconciliation cases a legacy finding can report.
FINDING_CASES = (
    "approved_no_balance_change",
    "employment_type_suspect",
    "hourly_vacation_leftover",
    "date_error",
    "stuck_cancelled",
    "refunded_marked_processed",
    "submitter_unmatched",
    "username_as_name",
    "no_user_link",
    "below_zero_predates_rules",
    "sharepoint_new_after_switch",
    "sharepoint_edit_after_switch",
    "sharepoint_delete_after_switch",
    "email_conflict",          # two staff records claim one email; the later one was blanked (#341 fix)
    "pending_unprocessed",     # a pending request the old pipeline never finished (no days or no manager)
    "unknown_status",          # a SharePoint Status outside the five known values (stored as cancelled)
)

# What HR did with a finding (legacy_findings.status).
FINDING_STATUSES = ("open", "resolved", "ignored")


def in_clause(column: str, allowed: tuple[str, ...]) -> str:
    """Build the SQL ``column IN ('a', 'b', ...)`` text for a CHECK constraint.

    The values are plain identifiers (no quotes or specials), so single-quoting
    them directly is safe and keeps the models and the migration reading from
    this one source.

    Args:
        column: The column name, already quoted if it is a reserved word (e.g. ``"case"``).
        allowed: The closed set of values the column may hold.

    Returns:
        The ``column IN (...)`` fragment for a CHECK constraint.
    """
    values = ", ".join(f"'{value}'" for value in allowed)      # 'a', 'b', ...
    return f"{column} IN ({values})"                            # column IN ('a', 'b', ...)
