"""Add staff_setup_nudge - who has been told their Staff Directory record is broken

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-31

Auxiliary state, like processing_log and dashboard_link_state - not part of the
SharePoint to Postgres migration. Records which broken record's creator has
already been nudged, what was wrong when they were told, and when the last
nudge went out, so the daily sweep re-nudges weekly rather than every morning
and stops entirely once the record is fixed.

Purely additive: creates one empty table. Rows appear as nudges go out, so
applying this on its own changes nothing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "staff_setup_nudge",
        # Staff Directory item id of the record whose setup is broken.
        sa.Column("employee_id", sa.String(), nullable=False),
        # Sorted fail codes joined by commas - the problem set as last emailed.
        sa.Column("issue_signature", sa.String(), nullable=False),
        # The record's creator, the only person ever nudged about it.
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("first_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("send_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("employee_id"),
    )


def downgrade() -> None:
    op.drop_table("staff_setup_nudge")
