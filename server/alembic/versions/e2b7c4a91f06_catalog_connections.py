"""catalog connections

Revision ID: e2b7c4a91f06
Revises: d9b4e2a71c58
Create Date: 2026-11-20 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e2b7c4a91f06"
down_revision: Union[str, Sequence[str], None] = "d9b4e2a71c58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "catalog_apps",
        sa.Column(
            "everyone",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_table(
        "catalog_app_bots",
        sa.Column("catalog_app_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_app_id"],
            ["catalog_apps.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("catalog_app_id", "bot_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("catalog_app_bots")
    op.drop_column("catalog_apps", "everyone")
