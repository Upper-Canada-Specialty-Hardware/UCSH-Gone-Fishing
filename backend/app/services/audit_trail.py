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
