"""routine runs

Revision ID: f7a2c6e81d04
Revises: 9e4b1c7a6d20
Create Date: 2026-11-18 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a2c6e81d04'
down_revision: Union[str, Sequence[str], None] = '9e4b1c7a6d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "routine_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'skipped')",
            name="routine_runs_status_check",
        ),
        sa.ForeignKeyConstraint(["routine_id"], ["routines.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("routine_id", "local_date"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("routine_runs")
