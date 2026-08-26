"""Add dashboard_link_state — when each person's dashboard link expires

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-26

Auxiliary state, like processing_log and request_approval_state — not part of
the SharePoint to Postgres migration. Records when each person was last emailed
a dashboard link and when that link stops validating, so the renewal task can
send a replacement before anyone loses access.

Purely additive: creates one empty table. Rows appear as emails go out and as
the renewal task seeds people it has not seen before, so applying this on its
own changes nothing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dashboard_link_state",
        # Staff Directory item id — the uid carried in the dashboard link.
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("employee_id"),
    )
    # The renewal task's only query is a range scan over expires_at.
    op.create_index(
        op.f("ix_dashboard_link_state_expires_at"),
        "dashboard_link_state",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dashboard_link_state_expires_at"), table_name="dashboard_link_state"
    )
    op.drop_table("dashboard_link_state")
