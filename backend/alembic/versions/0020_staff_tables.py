"""Add the Staff Directory tables: staff, links, lookups and the write-back queue

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-16

The Postgres home the Staff Directory moves into (#261): one typed ``staff``
table keyed on the SharePoint item id, a ``staff_supervisors`` table of the
manager and supervisor links between people, ``locations`` and ``departments``
lookup tables for the two choice columns, and a ``staff_writeback`` queue of
app-made changes to replay onto the SharePoint list. Runs on both Postgres and
SQLite (tests use create_all, so this file is only exercised against Postgres in
production, but it is written to apply on either).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Days and hours: two decimals, float not Decimal, matching the request tables.
_NUM = sa.Numeric(8, 2, asdecimal=False)
# A timezone-aware UTC timestamp column.
_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # The two choice columns as their own lookup lists.
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_locations_name"),
    )
    op.create_index("ix_locations_name", "locations", ["name"])

    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_departments_name"),
    )
    op.create_index("ix_departments_name", "departments", ["name"])

    # One row per person, typed, keyed on the SharePoint item id.
    op.create_table(
        "staff",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("cell_number", sa.String(), nullable=True),
        sa.Column("extension", sa.Integer(), nullable=True),
        sa.Column("birthday", sa.Date(), nullable=True),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("salary_hourly", sa.String(), nullable=True),
        sa.Column("supervisor_text", sa.String(), nullable=True),
        sa.Column("system_check", sa.String(), nullable=True),
        sa.Column("vacation_entitlement", _NUM, nullable=True),
        sa.Column("sick_entitlement", _NUM, nullable=True),
        sa.Column("vacation_balance", _NUM, nullable=True),
        sa.Column("sick_balance", _NUM, nullable=True),
        sa.Column("overtime_balance", _NUM, nullable=True),
        sa.Column("carry_over", _NUM, nullable=True),
        sa.Column("payout", _NUM, nullable=True),
        sa.Column("request_allow_date", sa.Date(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        sa.Column("title_link_sp_user_id", sa.Integer(), nullable=True),
        sa.Column("title_link_name", sa.String(), nullable=True),
        sa.Column("sp_modified", _TS, nullable=True),
        sa.Column("sp_created", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.UniqueConstraint("sp_item_id", name="uq_staff_sp_item_id"),
        # Non-null emails are unique (the by-email match); NULLs are allowed many
        # times, so people without an email never collide.
        sa.UniqueConstraint("email", name="uq_staff_email"),
    )
    op.create_index("ix_staff_sp_item_id", "staff", ["sp_item_id"])
    op.create_index("ix_staff_email", "staff", ["email"])

    # The manager and supervisor links between people.
    op.create_table(
        "staff_supervisors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        sa.Column("relation", sa.String(), nullable=False),
        sa.Column("manager_sp_user_id", sa.Integer(), nullable=True),
        sa.Column("manager_name", sa.String(), nullable=True),
        sa.Column("manager_staff_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"]),
        sa.ForeignKeyConstraint(["manager_staff_id"], ["staff.id"]),
    )
    op.create_index("ix_staff_supervisors_staff_id", "staff_supervisors", ["staff_id"])

    # The queue of app changes to replay onto the SharePoint list.
    op.create_table(
        "staff_writeback",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", _TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("sent_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_staff_writeback_sp_item_id", "staff_writeback", ["sp_item_id"])
    op.create_index("ix_staff_writeback_status_next", "staff_writeback", ["status", "next_attempt_at"])


def downgrade() -> None:
    # Reverse order: a table's foreign keys point at one created before it.
    op.drop_table("staff_writeback")
    op.drop_table("staff_supervisors")
    op.drop_table("staff")
    op.drop_table("departments")
    op.drop_table("locations")
