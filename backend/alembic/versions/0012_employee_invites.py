"""Add employee_invites - site access attempts from Add Employee

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07

Auxiliary state, like held_requests. One row per employee recording whether
Add Employee found them in Microsoft 365 or invited them as a guest, and
whether they were added to the site members group.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_invites",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("group_added", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # One row per employee, looked up by their Staff Directory id.
    op.create_index(op.f("ix_employee_invites_employee_id"), "employee_invites", ["employee_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_employee_invites_employee_id"), table_name="employee_invites")
    op.drop_table("employee_invites")
