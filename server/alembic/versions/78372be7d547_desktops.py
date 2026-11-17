"""desktops

Revision ID: 78372be7d547
Revises: c54ff0c6fb42
Create Date: 2026-11-17 22:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '78372be7d547'
down_revision: Union[str, Sequence[str], None] = 'c54ff0c6fb42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "desktops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("computer_id", sa.Uuid(), nullable=False),
        sa.Column("working", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "computer_id"),
    )
    op.create_index(
        "desktops_one_working",
        "desktops",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("working"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("desktops_one_working", table_name="desktops")
    op.drop_table("desktops")
