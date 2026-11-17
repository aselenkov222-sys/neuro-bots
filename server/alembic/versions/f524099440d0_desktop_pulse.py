"""desktop pulse

Revision ID: f524099440d0
Revises: 78372be7d547
Create Date: 2026-11-17 22:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f524099440d0'
down_revision: Union[str, Sequence[str], None] = '78372be7d547'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("desktops", sa.Column("timezone", sa.Text(), nullable=True))
    op.add_column(
        "desktops",
        sa.Column("last_pulse_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("desktops", "last_pulse_at")
    op.drop_column("desktops", "timezone")
