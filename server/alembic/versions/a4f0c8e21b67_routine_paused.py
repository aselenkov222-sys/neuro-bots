"""routine paused

Revision ID: a4f0c8e21b67
Revises: b7e2c4a91d58
Create Date: 2026-11-18 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4f0c8e21b67'
down_revision: Union[str, Sequence[str], None] = 'b7e2c4a91d58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "routines",
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("routines", "paused")
