"""Read/write the employee-entered description carried in a request's SP Title.

Leave-request Titles are **compound**. Both intake paths write
``"<employee name> /// <what the request is for>"``: the Microsoft Form ->
Power Automate flow ("01 New Leave Request" sets ``item/Title`` to
``"<name answer> /// <description answer>"``) and ``POST /forms/leave``. So a
leave Title cannot be shown to a manager as-is -- it would repeat the name
that already sits on the "Requested by" row of the email.

Overtime Titles need no splitting (the overtime flow maps a single form answer
straight to ``item/Title``, and that answer *is* the description), and
carry-over/payout requests have no description field at all. Leave is the only
compound case, which is why ``request_type`` selects the behaviour.

``extract_request_description`` mirrors ``getDescription`` in
frontend/src/components/dataGridDefaults.ts -- the dashboard grids and the
emails must show the same text for the same request, so keep the two in step.
"""

LEAVE_TITLE_SEPARATOR = " /// "


def compose_leave_title(employee_name: str | None, notes: str | None) -> str:
    """Build the compound Title stored on a new leave request.

    The separator is emitted whenever there are notes -- even when the name is
    blank -- so that ``extract_request_description`` can always find the
    description again. (A Title with no separator means "name only, no
    description", which is how a request submitted without notes is stored.)
    """
    name = (employee_name or "").strip()
    note = (notes or "").strip()
    if not note:
        return name
    return f"{name}{LEAVE_TITLE_SEPARATOR}{note}"


def extract_request_description(title: str | None, request_type: str = "leave") -> str:
    """Return just the employee-entered description from a request Title.

    Leave: everything after the *first* separator, so a description that itself
    contains " /// " survives intact. No separator means the Title is a bare
    name, which carries no description -- hence "".

    Any other request type: the Title as-is.

    The separator is matched before any trimming, so a Title with an empty name
    half (" /// Doctor's appointment") still yields its description rather than
    losing the leading space that the separator needs.
    """
    raw = title or ""
    if request_type == "leave":
        _name, separator, description = raw.partition(LEAVE_TITLE_SEPARATOR)
        return description.strip() if separator else ""
    return raw.strip()
