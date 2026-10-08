"""Add email_codes - one-time sign-in codes for the public request page

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07

Auxiliary state, like processing_log and dashboard_link_state. A person proves
they own an email address by typing back a 6-digit code sent to it; each row is
one code sent. Only an HMAC of the code is stored.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("requester_ip", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Every check and rate-limit count filters by email, by IP, or by time.
    op.create_index(op.f("ix_email_codes_email"), "email_codes", ["email"], unique=False)
    op.create_index(
        op.f("ix_email_codes_requester_ip"), "email_codes", ["requester_ip"], unique=False
    )
    op.create_index(
        op.f("ix_email_codes_created_at"), "email_codes", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_email_codes_created_at"), table_name="email_codes")
    op.drop_index(op.f("ix_email_codes_requester_ip"), table_name="email_codes")
    op.drop_index(op.f("ix_email_codes_email"), table_name="email_codes")
    op.drop_table("email_codes")
