"""Add salary_hourly and cell_number to employees

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30

0005 ported the Staff Directory into ``employees`` but missed two fields the
app actually reads, both of which the employees cutover needs:

  * ``salary_hourly`` (SP ``SalaryHourly``) — read at five call sites to decide
    whether an approval touches balances at all. Without it the value reads as
    NULL, ``!= "Hourly"``, and hourly staff would silently start having
    vacation deducted.
  * ``cell_number`` (SP ``CellNumber``) — the recipient of the manager approval
    SMS. Without it managers quietly stop receiving texts.

Purely additive and nullable. Nothing reads the employees table until
STORAGE_EMPLOYEES is flipped, so applying this changes no behaviour.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("salary_hourly", sa.String(), nullable=True))
    op.add_column("employees", sa.Column("cell_number", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "cell_number")
    op.drop_column("employees", "salary_hourly")
