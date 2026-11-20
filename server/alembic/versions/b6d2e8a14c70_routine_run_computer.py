"""routine run computer

Revision ID: b6d2e8a14c70
Revises: a9c3e7b14d82
Create Date: 2026-11-20 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6d2e8a14c70"
down_revision: Union[str, Sequence[str], None] = "a9c3e7b14d82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "routine_runs",
        sa.Column("computer_id", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("routine_runs", "computer_id")
