import json
import logging
from datetime import datetime, timezone

from app.graph.sharepoint import sp_client

logger = logging.getLogger(__name__)


def snapshot_balances(fields: dict) -> dict:
    """Extract balance values from SP employee fields."""
    return {
        "CurrentVacationBalance": float(fields.get("CurrentVacationBalance", 0) or 0),
        "CurrentSickDayBalance": float(fields.get("CurrentSickDayBalance", 0) or 0),
        "CurrentOvertimeBalance": float(fields.get("CurrentOvertimeBalance", 0) or 0),
        "CarryOver": float(fields.get("CarryOver", 0) or 0),
        "Payout": float(fields.get("Payout", 0) or 0),
    }


class AuditTrailBuilder:
    """Accumulates balance-change steps during an approval or refund flow."""

    def __init__(self, action: str):
        self.action = action
        self.steps: list[dict] = []

    def add_step(
        self,
        operation: str,
        before: dict,
        after: dict,
        detail: str | None = None,
    ):
        step: dict = {"operation": operation, "before": before, "after": after}
        if detail:
            step["detail"] = detail
        self.steps.append(step)

    def build_entry(self) -> dict:
        return {
            "action": self.action,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "steps": self.steps,
        }


def describe_cascade_changes(before: dict, after: dict) -> str:
    """Generate a human-readable description of what the cascade changed."""
    changes = []
    for key in before:
        if key in after and before[key] != after[key]:
            diff = after[key] - before[key]
            direction = "added to" if diff > 0 else "taken from"
            changes.append(f"{abs(diff)} {direction} {key}")
    return "; ".join(changes) if changes else "No changes needed"


def extract_approval_deltas(
    raw_audit_log: str,
    balance_keys: tuple[str, ...] = (
        "CurrentVacationBalance",
        "CurrentSickDayBalance",
        "CurrentOvertimeBalance",
        "CarryOver",
    ),
) -> dict[str, float] | None:
    """Parse BalanceAuditLog JSON, find last approve entry, return net deltas per balance key.

    Returns None if parsing fails, no approve entry exists, or all deltas are zero.
    """
    try:
        entries = json.loads(raw_audit_log) if raw_audit_log and raw_audit_log.strip() else []
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(entries, list):
        return None

    # Find the last approve entry
    approve_entry = None
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get("action") == "approve":
            approve_entry = entry
            break

    if not approve_entry:
        return None

    deltas: dict[str, float] = {key: 0.0 for key in balance_keys}
    for step in approve_entry.get("steps", []):
        before = step.get("before", {})
        after = step.get("after", {})
        for key in balance_keys:
            if key in before and key in after:
                deltas[key] += float(after[key]) - float(before[key])

    # Filter to non-zero deltas
    result = {k: v for k, v in deltas.items() if v != 0.0}
    return result if result else None


async def write_audit_log(
    list_id: str, item_id: str | int, builder: AuditTrailBuilder
) -> None:
    """Read existing BalanceAuditLog, append new entry, write back.

    Wrapped in try/except so it never blocks the approval/refund flow.
    """
    try:
        item = await sp_client.get_list_item(list_id, item_id)
        raw = item["fields"].get("BalanceAuditLog", "") or ""

        existing = json.loads(raw) if raw.strip() else []
        existing.append(builder.build_entry())

        await sp_client.update_list_item_fields(
            list_id, item_id, {"BalanceAuditLog": json.dumps(existing)}
        )
    except Exception:
        logger.exception("Failed to write audit log for %s item %s", list_id, item_id)
