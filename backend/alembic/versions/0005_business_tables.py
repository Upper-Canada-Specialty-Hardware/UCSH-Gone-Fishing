"""Add migrated business tables (employees, requests, holidays, manager assignments)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-02

Purely additive: creates the Postgres homes for the data currently living in
SharePoint. Nothing reads or writes these tables yet (that comes with the
repository cutovers), so applying this migration does not change app behavior.

Note: Alembic's autogenerate also flagged pre-existing NOT NULL drift on the
existing plumbing tables (change_tokens.updated_at, processing_log.processed_at,
etc.). That is intentionally NOT included here — it is unrelated to this feature
and would alter live production columns. Any such cleanup belongs in its own PR.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employees",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("department", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("employee_type", sa.String(), nullable=True),
        sa.Column("vacation_balance", sa.Float(), nullable=False),
        sa.Column("sick_balance", sa.Float(), nullable=False),
        sa.Column("overtime_balance", sa.Float(), nullable=False),
        sa.Column("carryover_balance", sa.Float(), nullable=False),
        sa.Column("payout_balance", sa.Float(), nullable=False),
        sa.Column("vacation_entitlement", sa.Float(), nullable=False),
        sa.Column("sick_entitlement", sa.Float(), nullable=False),
        sa.Column("request_allow_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_employees_email"), "employees", ["email"], unique=False)
    op.create_index(op.f("ix_employees_sp_item_id"), "employees", ["sp_item_id"], unique=True)
    op.create_index(op.f("ix_employees_sp_user_lookup_id"), "employees", ["sp_user_lookup_id"], unique=False)

    op.create_table(
        "manager_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("manager_sp_user_lookup_id", sa.Integer(), nullable=False),
        sa.Column("manager_name", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "manager_sp_user_lookup_id", name="uq_manager_assignment"),
    )
    op.create_index(op.f("ix_manager_assignments_employee_id"), "manager_assignments", ["employee_id"], unique=False)

    op.create_table(
        "leave_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("leave_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("approve_processed_flag", sa.String(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("days", sa.Float(), nullable=True),
        sa.Column("partial_hours", sa.Float(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("submitter_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("submitter_name", sa.String(), nullable=True),
        sa.Column("manager_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("staff_location", sa.String(), nullable=True),
        sa.Column("staff_department", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_leave_requests_sp_item_id"), "leave_requests", ["sp_item_id"], unique=True)
    op.create_index(op.f("ix_leave_requests_submitter_sp_user_lookup_id"), "leave_requests", ["submitter_sp_user_lookup_id"], unique=False)

    op.create_table(
        "overtime_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("hours", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("submitter_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("submitter_name", sa.String(), nullable=True),
        sa.Column("manager_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_overtime_requests_sp_item_id"), "overtime_requests", ["sp_item_id"], unique=True)
    op.create_index(op.f("ix_overtime_requests_submitter_sp_user_lookup_id"), "overtime_requests", ["submitter_sp_user_lookup_id"], unique=False)

    op.create_table(
        "carryover_payout_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("type_of_request", sa.String(), nullable=True),
        sa.Column("days", sa.Float(), nullable=True),
        sa.Column("system_state", sa.String(), nullable=True),
        sa.Column("submitter_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("employee_sp_item_id", sa.String(), nullable=True),
        sa.Column("manager_sp_user_lookup_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_carryover_payout_requests_employee_sp_item_id"), "carryover_payout_requests", ["employee_sp_item_id"], unique=False)
    op.create_index(op.f("ix_carryover_payout_requests_sp_item_id"), "carryover_payout_requests", ["sp_item_id"], unique=True)
    op.create_index(op.f("ix_carryover_payout_requests_submitter_sp_user_lookup_id"), "carryover_payout_requests", ["submitter_sp_user_lookup_id"], unique=False)

    op.create_table(
        "holidays",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sp_item_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("date", sa.Date(), nullable=True),
        sa.Column("province", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_holidays_date"), "holidays", ["date"], unique=False)
    op.create_index(op.f("ix_holidays_province"), "holidays", ["province"], unique=False)
    op.create_index(op.f("ix_holidays_sp_item_id"), "holidays", ["sp_item_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_holidays_sp_item_id"), table_name="holidays")
    op.drop_index(op.f("ix_holidays_province"), table_name="holidays")
    op.drop_index(op.f("ix_holidays_date"), table_name="holidays")
    op.drop_table("holidays")

    op.drop_index(op.f("ix_carryover_payout_requests_submitter_sp_user_lookup_id"), table_name="carryover_payout_requests")
    op.drop_index(op.f("ix_carryover_payout_requests_sp_item_id"), table_name="carryover_payout_requests")
    op.drop_index(op.f("ix_carryover_payout_requests_employee_sp_item_id"), table_name="carryover_payout_requests")
    op.drop_table("carryover_payout_requests")

    op.drop_index(op.f("ix_overtime_requests_submitter_sp_user_lookup_id"), table_name="overtime_requests")
    op.drop_index(op.f("ix_overtime_requests_sp_item_id"), table_name="overtime_requests")
    op.drop_table("overtime_requests")

    op.drop_index(op.f("ix_leave_requests_submitter_sp_user_lookup_id"), table_name="leave_requests")
    op.drop_index(op.f("ix_leave_requests_sp_item_id"), table_name="leave_requests")
    op.drop_table("leave_requests")

    op.drop_index(op.f("ix_manager_assignments_employee_id"), table_name="manager_assignments")
    op.drop_table("manager_assignments")

    op.drop_index(op.f("ix_employees_sp_user_lookup_id"), table_name="employees")
    op.drop_index(op.f("ix_employees_sp_item_id"), table_name="employees")
    op.drop_index(op.f("ix_employees_email"), table_name="employees")
    op.drop_table("employees")
