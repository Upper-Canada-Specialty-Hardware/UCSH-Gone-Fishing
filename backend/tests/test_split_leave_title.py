"""Splitting a leave Title into its description and bereavement relation (#321).

Leave Titles come in two shapes: the app writes "<name> /// <notes>" and the
Microsoft Form writes "<description> /// <relation>" (or a bare "<description>"
for a part day). split_leave_title tells them apart by comparing the left half
to the employee name, case-insensitively and trimmed.
"""

from app.services.request_descriptions import split_leave_title


def test_app_format_takes_the_description_from_the_right_half():
    # Left half is the name, so the notes are on the right; no relation.
    assert split_leave_title("Pat Staff /// Doctor appt", "Pat Staff", "Vacation") == ("Doctor appt", None)


def test_form_format_with_a_bereavement_relation():
    # Left half is not the name: Form shape, and bereavement makes the right half the relation.
    assert split_leave_title(
        "Grandmother passed /// Grandmother", "Pat Staff", "Bereavement"
    ) == ("Grandmother passed", "Grandmother")


def test_form_format_without_a_relation_appends_the_right_half():
    # Not the name and not bereavement: the right half is more description, appended on.
    assert split_leave_title("Doctor /// afternoon", "Pat Staff", "Vacation") == ("Doctor afternoon", None)


def test_bereavement_with_no_relation_half_leaves_the_relation_empty():
    # A separator but a blank right half for bereavement: description kept, no relation.
    assert split_leave_title("Funeral /// ", "Pat Staff", "Bereavement") == ("Funeral", None)


def test_a_bare_name_has_no_description():
    # The whole Title is just the employee's name: nothing was entered.
    assert split_leave_title("Pat Staff", "Pat Staff", "Vacation") == ("", None)


def test_a_bare_description_is_the_whole_title():
    # No separator and not the name: the Form wrote a bare description (part day).
    assert split_leave_title("Doctor appointment", "Pat Staff", "Vacation") == ("Doctor appointment", None)


def test_case_and_surrounding_spaces_in_the_name_still_match():
    # The name match is case-insensitive and trimmed, so this is still app format.
    assert split_leave_title("  PAT STAFF  /// Doctor", "pat staff", "Vacation") == ("Doctor", None)


def test_a_blank_left_half_is_the_name_less_app_shape():
    # compose_leave_title with a blank name emits a leading separator; the right
    # half is still the description.
    assert split_leave_title(" /// Doctor", "Pat Staff", "Vacation") == ("Doctor", None)
