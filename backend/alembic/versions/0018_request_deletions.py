"""Add request_deletions - a record of each request an admin deleted (#282)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-14

Auxiliary state, like held_requests. Deleting a request removes the list item
for good, so the whole item (as JSON), the reason the admin gave, and whether
its balance change was refunded are kept here.

Follows 0017 (staff_changes, #233).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_deletions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_type", sa.String(), nullable=False),
        sa.Column("list_item_id", sa.String(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("request_deletions")
