"""dangerous steps

Revision ID: b3c7e9a41d52
Revises: f3a8c1d27e05
Create Date: 2026-11-19 08:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3c7e9a41d52"
down_revision: Union[str, Sequence[str], None] = "f3a8c1d27e05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RUN_OPEN = "status IN ('pending', 'claimed', 'skipped', 'cancelled', 'failed')"
_RUN_GATED = (
    "status IN ('pending', 'claimed', 'skipped', 'cancelled', 'failed', "
    "'awaiting', 'finished')"
)
_CHECK_OPEN = "status IN ('pending', 'claimed')"
_CHECK_GATED = "status IN ('pending', 'claimed', 'awaiting')"
_NOTICE_OPEN = "kind IN ('completed', 'failed', 'skipped')"
_NOTICE_GATED = "kind IN ('completed', 'failed', 'skipped', 'waiting')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _RUN_GATED
    )
    op.drop_constraint(
        "routine_checks_status_check", "routine_checks", type_="check"
    )
    op.create_check_constraint(
        "routine_checks_status_check", "routine_checks", _CHECK_GATED
    )
    op.drop_constraint(
        "desktop_notices_kind_check", "desktop_notices", type_="check"
    )
    op.create_check_constraint(
        "desktop_notices_kind_check", "desktop_notices", _NOTICE_GATED
    )
    op.create_table(
        "dangerous_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("check_id", sa.Uuid(), nullable=True),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("answer", sa.String(length=3), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "answer IS NULL OR answer IN ('yes', 'no')",
            name="dangerous_steps_answer_check",
        ),
        sa.CheckConstraint(
            "(run_id IS NOT NULL AND check_id IS NULL) OR "
            "(run_id IS NULL AND check_id IS NOT NULL)",
            name="dangerous_steps_parent_check",
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"]),
        sa.ForeignKeyConstraint(
            ["check_id"], ["routine_checks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(
            ["run_id"], ["routine_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("dangerous_steps")
    op.drop_constraint(
        "desktop_notices_kind_check", "desktop_notices", type_="check"
    )
    op.create_check_constraint(
        "desktop_notices_kind_check", "desktop_notices", _NOTICE_OPEN
    )
    op.drop_constraint(
        "routine_checks_status_check", "routine_checks", type_="check"
    )
    op.create_check_constraint(
        "routine_checks_status_check", "routine_checks", _CHECK_OPEN
    )
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _RUN_OPEN
    )
