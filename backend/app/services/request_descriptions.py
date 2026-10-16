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


def split_leave_title(
    title: str | None, employee_name: str | None, leave_type: str | None
) -> tuple[str, str | None]:
    """Split a leave Title into its description and bereavement relation.

    Leave Titles come in two shapes, and telling them apart is the descriptions
    bug (#321): the app writes ``"<employee name> /// <notes>"`` while the
    Microsoft Form writes ``"<description> /// <bereavement relation>"`` (or a
    bare ``"<description>"`` for a part day with no relation). The left half is
    compared to the employee name, case-insensitively and trimmed, to decide
    which shape it is.

    Args:
        title: The stored Title, or None.
        employee_name: The request's employee name, for the comparison.
        leave_type: The leave type, which decides whether a Form right half is a
            relation (Bereavement) or more description.

    Returns:
        ``(description, relation)``: the employee-entered description (``""`` when
        there is none) and the bereavement relation, or None when there is none.
    """
    raw = title or ""
    name = (employee_name or "").strip()                        # the name to match the left half against
    left, separator, right = raw.partition(LEAVE_TITLE_SEPARATOR)
    left_half = left.strip()
    right_half = right.strip()

    if not separator:
        # No separator: a bare name carries no description; anything else is one.
        if raw.strip().lower() == name.lower():
            return "", None                                     # the Title is just the employee's name
        return raw.strip(), None                                # a bare Form description (part day, no relation)

    # App format: the left half is the employee's name (or blank, name-less app).
    if left_half == "" or left_half.lower() == name.lower():
        return right_half, None                                 # the notes are the right half; no relation

    # Form format: the left half is the description.
    if leave_type == "Bereavement":
        return left_half, (right_half or None)                  # the right half is the relation
    # A non-bereavement right half is extra description, appended on.
    description = f"{left_half} {right_half}".strip() if right_half else left_half
    return description, None
