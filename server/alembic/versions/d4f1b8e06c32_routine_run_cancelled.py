"""routine run cancelled

Revision ID: d4f1b8e06c32
Revises: a1c4e8b27d60
Create Date: 2026-11-18 17:45:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd4f1b8e06c32'
down_revision: Union[str, Sequence[str], None] = 'a1c4e8b27d60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OPEN = "status IN ('pending', 'claimed', 'skipped')"
_MARKED = "status IN ('pending', 'claimed', 'skipped', 'cancelled')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _MARKED
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "routine_runs_status_check", "routine_runs", type_="check"
    )
    op.create_check_constraint(
        "routine_runs_status_check", "routine_runs", _OPEN
    )
