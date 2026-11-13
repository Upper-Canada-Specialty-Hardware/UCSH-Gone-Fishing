"""Year-end carry log (#553)

One row per person and ended year, so the automatic January 1 carry-over runs
once for each person even if the task restarts mid-run.

Postgres only: SQLite (tests) builds its schema from the models with
``create_all``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create ``year_end_carry_log``."""
    bind = op.get_bind()                                       # branch on the dialect
    if bind.dialect.name != "postgresql":
        return                                                 # SQLite builds from the models via create_all
    op.create_table(
        "year_end_carry_log",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    """Drop ``year_end_carry_log``."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_table("year_end_carry_log")
