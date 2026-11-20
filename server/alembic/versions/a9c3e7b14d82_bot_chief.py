"""bot chief

Revision ID: a9c3e7b14d82
Revises: e2b7c4a91f06
Create Date: 2026-11-20 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a9c3e7b14d82"
down_revision: Union[str, Sequence[str], None] = "e2b7c4a91f06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "bots",
        sa.Column(
            "chief",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "bots_one_chief",
        "bots",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("chief"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("bots_one_chief", table_name="bots")
    op.drop_column("bots", "chief")
