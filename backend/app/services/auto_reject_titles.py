"""Helper for appending an auto-reject reason tag to a SharePoint Title field.

The same reason sentence built at each auto-reject site is also passed into
the corresponding email template, so the employee sees identical wording in
their inbox and on the SP list view.
"""

_MAX_TITLE_LEN = 255  # SharePoint single-line text default cap


def append_auto_reject_tag(original_title: str | None, reason: str) -> str:
    base = (original_title or "").rstrip()
    tag = f"[Auto-Rejected: {reason}]"
    combined = f"{base} {tag}" if base else tag

    if len(combined) <= _MAX_TITLE_LEN:
        return combined

    # Truncate the original portion — the tag (and reason) must survive intact.
    budget = _MAX_TITLE_LEN - len(tag) - 2  # 1 space + 1 ellipsis
    if budget <= 0:
        return tag[:_MAX_TITLE_LEN]
    base = base[:budget].rstrip() + "…"
    return f"{base} {tag}"
