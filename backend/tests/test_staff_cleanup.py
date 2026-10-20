"""The pure helpers that tidy typed staff identity fields before they are saved.

A stray space in a name (the Glenn Clarke case) or a capital in an email breaks
the exact matches the request path makes, so these pin exactly what collapses
and what is left alone.
"""

from app.services.staff_cleanup import clean_email, clean_employee_form, collapse_whitespace


def test_collapse_trims_ends_and_squeezes_runs_including_non_breaking():
    assert collapse_whitespace("  Glenn  Clarke ") == "Glenn Clarke"
    assert collapse_whitespace("Ware   house") == "Ware house"
    assert collapse_whitespace(None) == ""


def test_collapse_keeps_a_single_internal_space():
    assert collapse_whitespace("Glenn Clarke") == "Glenn Clarke"


def test_clean_email_removes_all_whitespace_and_folds_case():
    assert clean_email("  Glenn@UCSH.com ") == "glenn@ucsh.com"
    assert clean_email("a b c@x.com") == "abc@x.com"
    assert clean_email(None) == ""


def test_clean_employee_form_tidies_only_identity_fields_and_reports_changes():
    cleaned, changes = clean_employee_form({
        "title": "Glenn  Clarke ",
        "email_address": "Glenn@UCSH.com",
        "department": "Sales",            # already clean -> no change
        "vacation_entitlement": 15,       # not text -> untouched
    })
    assert cleaned["title"] == "Glenn Clarke"
    assert cleaned["email_address"] == "glenn@ucsh.com"
    assert cleaned["department"] == "Sales"
    assert cleaned["vacation_entitlement"] == 15
    # Only the two fields that actually moved are reported.
    moved = {c["field"]: (c["before"], c["after"]) for c in changes}
    assert moved == {
        "title": ("Glenn  Clarke ", "Glenn Clarke"),
        "email_address": ("Glenn@UCSH.com", "glenn@ucsh.com"),
    }
