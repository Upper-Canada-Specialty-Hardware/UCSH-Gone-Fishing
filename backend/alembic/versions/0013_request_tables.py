"""Add the three request tables (leave, overtime, carry-over/payout)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-09

Postgres homes for the three SharePoint request lists. Each row is one list
item: ``id`` is the item id (a copied request keeps its SharePoint id),
``fields`` holds every column value under its SharePoint column name, and
``sp_item_id`` names the SharePoint item a row came from, if any. Nothing reads
these tables until STORAGE_REQUESTS is set to "postgres".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# One table per SharePoint list, all the same shape.
TABLES = ("leave_requests", "overtime_requests", "carryover_payout_requests")


def upgrade() -> None:
    for table in TABLES:
        op.create_table(
            table,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fields", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sp_item_id", sa.String(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("sp_item_id", name=f"uq_{table}_sp_item_id"),   # one row per SharePoint item
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
