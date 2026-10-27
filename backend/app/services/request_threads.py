"""One email subject per request, so every email about it lands in one thread (#520).

Outlook groups a conversation by its subject line (ignoring RE: and FW:). Each
step used to have its own subject ("Leave Request - name", "Reminder: ...",
"name - Leave Request: Approved"), so a manager or employee got a new thread for
every step. Now every email about one request (the manager's ask, reminders,
the receipt, the result, a refund, the HR bereavement alert, a blocked notice)
carries the same subject, and the step is said in the body instead.

The subject is worked out once, when the request is submitted, and kept in the
request's ``extra`` under ``ThreadSubject``, so a later change (an admin switching
carry-over to payout, a name fix on the staff record) can never split the thread.
A request submitted before this keeps no stored subject; it gets the same wording
built from its row, which matches as long as those fields never changed.
"""

# Where the subject is kept on the request row (its extra JSON, SharePoint-column style).
SUBJECT_KEY = "ThreadSubject"

# A request kind -> the words the subject opens with.
_KIND_WORDS = {"leave": "Leave request", "overtime": "Time make-up request"}

# A carry-over/payout row's stored option -> the words the subject opens with.
_OPTION_WORDS = {"carry_over": "Carry-over request", "payout": "Payout request"}


def build_thread_subject(
    kind: str, item_id, employee_name: str | None, request_option: str | None = None,
) -> str:
    """The subject every email about one request uses, e.g. "Leave request #3930 for Jane Doe".

    The request number keeps two requests by the same person in separate threads.
    Nothing that an edit can change (dates, days, leave type) goes in it.

    Args:
        kind: "leave", "overtime" or "carryover_payout".
        item_id: The request number people and links use.
        employee_name: Who the request is for; blank leaves the "for" part off.
        request_option: "carry_over" or "payout" for a carry-over/payout row.

    Returns:
        The subject line.
    """
    if kind == "carryover_payout":
        words = _OPTION_WORDS.get(request_option or "", "Carry-over or payout request")   # unknown option: both words
    else:
        words = _KIND_WORDS.get(kind, "Request")                  # an unknown kind still reads plainly
    name = (employee_name or "").strip()
    return f"{words} #{item_id} for {name}" if name else f"{words} #{item_id}"


def thread_subject(record) -> str:
    """The request's one subject: the stored one, else built from the row.

    Args:
        record: The ``RequestRecord``.

    Returns:
        The subject every email about this request uses.
    """
    stored = (record.extra or {}).get(SUBJECT_KEY)                # fixed at submit
    if stored:
        return stored
    return build_thread_subject(record.kind, record.item_id, record.employee_name, record.request_option)


def stamp_thread_subject(record) -> None:
    """Fix the request's subject on the row, once, so later changes cannot move it.

    Called at submit, after the submitter's name is stamped. A row that already
    has a subject is left alone, so a resubmit or reprocess never changes it.

    Args:
        record: The ``RequestRecord``; changed in place, not committed.
    """
    extra = dict(record.extra or {})
    if extra.get(SUBJECT_KEY):                                   # already fixed: never rewrite it
        return
    extra[SUBJECT_KEY] = build_thread_subject(
        record.kind, record.item_id, record.employee_name, record.request_option,
    )
    record.extra = extra                                         # a new dict so the JSON change is saved
