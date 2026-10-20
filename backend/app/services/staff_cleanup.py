"""Tidy the free-typed staff identity fields before they are saved or compared.

A Staff Directory name typed with a trailing space ("Glenn Clarke ") or a
doubled space ("Glenn  Clarke") still looks right to a person, but the request
path matches a name EXACTLY, so a single stray character routes the person to
the wrong manager (or to nobody). An email typed with a capital or a stray space
is the same hazard for the request page, which finds a person by email.

These are pure helpers (no I/O), so every save path and every duplicate check can
run a value through the same normalisation and the stored data stays consistent.
Names and departments keep their internal single spaces; emails carry none.
"""

import re

# Matches one run of whitespace. In Unicode (str) mode \s also matches the
# non-breaking space U+00A0 and its relatives, so a value pasted from Word or a
# web form is collapsed the same as one typed with ordinary spaces.
_WHITESPACE_RUN = re.compile(r"\s+")

# The snake_case form keys whose typed text is cleaned, and the cleaner each one
# uses. Kept here so the create form, the check endpoint and the edit paths all
# agree on which fields are tidied and how.
NAME_LIKE_KEYS = ("title", "department")
EMAIL_KEYS = ("email_address",)


def collapse_whitespace(value) -> str:
    """Trim the ends and squeeze every run of whitespace down to one space.

    Used for names and departments: their words stay separated by a single
    space, while leading, trailing and doubled spaces (ordinary or non-breaking)
    are removed.

    Args:
        value: The submitted or stored value; None counts as blank.

    Returns:
        The value as a tidied string: no leading or trailing space, and no run
        of whitespace longer than one ordinary space.
    """
    text = "" if value is None else str(value)       # None -> "", never the word "None"
    squeezed = _WHITESPACE_RUN.sub(" ", text)         # every whitespace run -> one space
    return squeezed.strip()                           # drop the single space the run may leave at an end


def clean_email(value) -> str:
    """Strip every space out of an email and lower-case it.

    An email carries no internal spaces, so whitespace anywhere in it is noise;
    lower-casing makes two records that differ only in case read as the same
    address, which is how the request page already compares them.

    Args:
        value: The submitted or stored value; None counts as blank.

    Returns:
        The address with all whitespace removed and every letter lower-case.
    """
    text = "" if value is None else str(value)        # None -> "", never the word "None"
    return _WHITESPACE_RUN.sub("", text).lower()      # remove ALL whitespace, then fold case


def clean_employee_form(form_data: dict) -> tuple[dict, list[dict]]:
    """Return a cleaned copy of a submitted employee form and what cleaning moved.

    Only the identity text fields are touched (name, department, email); every
    other key is passed through unchanged. Non-string values are left alone, so
    a numeric balance is never coerced here.

    Args:
        form_data: The submitted form, snake_case keys.

    Returns:
        A ``(cleaned, changes)`` pair. ``cleaned`` is a new dict with the
        identity fields tidied. ``changes`` is one ``{"field", "before",
        "after"}`` per field whose value cleaning actually changed, so the
        confirm panel can show the person exactly what will be saved.
    """
    cleaned = dict(form_data)                          # copy; never mutate the caller's dict
    changes: list[dict] = []                           # only fields cleaning actually moved
    for key in NAME_LIKE_KEYS + EMAIL_KEYS:            # the identity fields, in a stable order
        before = form_data.get(key)                    # the raw submitted value (may be missing)
        if not isinstance(before, str):                # only tidy text the person typed
            continue
        after = clean_email(before) if key in EMAIL_KEYS else collapse_whitespace(before)
        cleaned[key] = after                           # store the tidied value either way
        if after != before:                            # record it only when it actually moved
            changes.append({"field": key, "before": before, "after": after})
    return cleaned, changes
