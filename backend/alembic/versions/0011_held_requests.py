"""Add held_requests - requests waiting for their submitter to be added

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07

Auxiliary state, like email_codes. A request made on the public request page
by someone not in the Staff Directory yet is kept here until their supervisor
adds them, then submitted as a normal request.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "held_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("supervisor_id", sa.String(), nullable=False),
        sa.Column("supervisor_name", sa.String(), nullable=False),
        sa.Column("supervisor_email", sa.String(), nullable=False),
        sa.Column("request_type", sa.String(), nullable=False),
        sa.Column("form_data", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supervisor_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admins_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sp_item_id", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # Adding an employee looks up by email; the reminder sweep by status.
    op.create_index(op.f("ix_held_requests_email"), "held_requests", ["email"], unique=False)
    op.create_index(op.f("ix_held_requests_status"), "held_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_held_requests_status"), table_name="held_requests")
    op.drop_index(op.f("ix_held_requests_email"), table_name="held_requests")
    op.drop_table("held_requests")
