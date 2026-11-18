"""routine runs cascade

Revision ID: a1c4e8b27d60
Revises: f7a2c6e81d04
Create Date: 2026-11-18 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1c4e8b27d60'
down_revision: Union[str, Sequence[str], None] = 'f7a2c6e81d04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "routine_runs_routine_id_fkey",
        "routine_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "routine_runs_routine_id_fkey",
        "routine_runs",
        "routines",
        ["routine_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "routine_runs_routine_id_fkey",
        "routine_runs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "routine_runs_routine_id_fkey",
        "routine_runs",
        "routines",
        ["routine_id"],
        ["id"],
    )
