"""Add app_settings - settings an admin changes from the dashboard

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-12

Auxiliary state. The first setting is carryover_payout_open, the switch that
opens and closes carry-over and payout requests (#203). A missing row means
the setting's default, so nothing is seeded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
