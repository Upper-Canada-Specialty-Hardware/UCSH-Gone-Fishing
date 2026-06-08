"""Add request_approval_state table for admin-edit versioning

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_approval_state",
        sa.Column("list_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("current_snapshot", sa.JSON(), nullable=False),
        sa.Column("previous_snapshot", sa.JSON(), nullable=True),
        sa.Column("last_emailed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("list_id", "item_id"),
    )


def downgrade() -> None:
    op.drop_table("request_approval_state")
