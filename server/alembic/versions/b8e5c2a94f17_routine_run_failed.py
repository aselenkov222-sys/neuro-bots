"""routine run failed

Revision ID: b8e5c2a94f17
Revises: d4f1b8e06c32
Create Date: 2026-11-18 17:55:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b8e5c2a94f17'
down_revision: Union[str, Sequence[str], None] = 'd4f1b8e06c32'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MARKED = "status IN ('pending', 'claimed', 'skipped', 'cancelled')"
_FAILED = (
    "status IN ('pending', 'claimed', 'skipped', 'cancelled', 'failed')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _FAILED
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _MARKED
    )
