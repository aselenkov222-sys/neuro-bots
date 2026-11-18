"""desktop routines stopped

Revision ID: 9e4b1c7a6d20
Revises: a4f0c8e21b67
Create Date: 2026-11-18 17:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e4b1c7a6d20'
down_revision: Union[str, Sequence[str], None] = 'a4f0c8e21b67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("desktops", sa.Column("session_id", sa.Uuid(), nullable=True))
    op.add_column(
        "desktops",
        sa.Column(
            "routines_stopped",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("desktops", "routines_stopped")
    op.drop_column("desktops", "session_id")
