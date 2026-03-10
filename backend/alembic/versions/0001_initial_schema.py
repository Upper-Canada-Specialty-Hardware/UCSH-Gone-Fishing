"""Initial schema — webhook_subscriptions, change_tokens, processing_log

Revision ID: 0001
Revises:
Create Date: 2026-03-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("list_id", sa.String(), nullable=False),
        sa.Column("expiration", sa.DateTime(), nullable=False),
        sa.Column("client_state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "change_tokens",
        sa.Column("list_id", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("list_id"),
    )

    op.create_table(
        "processing_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("list_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("list_id", "item_id", "action", name="uq_processing_log"),
    )


def downgrade() -> None:
    op.drop_table("processing_log")
    op.drop_table("change_tokens")
    op.drop_table("webhook_subscriptions")
