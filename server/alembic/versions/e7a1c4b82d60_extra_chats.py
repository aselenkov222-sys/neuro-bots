"""extra chats

Revision ID: e7a1c4b82d60
Revises: b3c7e9a41d52
Create Date: 2026-11-19 10:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7a1c4b82d60"
down_revision: Union[str, Sequence[str], None] = "b3c7e9a41d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("chats", sa.Column("kind", sa.String(length=16), nullable=True))
    op.add_column(
        "chats",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE chats SET kind = 'main', created_at = updated_at")
    op.alter_column("chats", "kind", nullable=False)
    op.alter_column("chats", "created_at", nullable=False)
    op.create_check_constraint(
        "chats_kind_check",
        "chats",
        "kind IN ('main', 'extra')",
    )
    op.create_index(
        "chats_one_main",
        "chats",
        ["bot_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'main'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("chats_one_main", table_name="chats")
    op.drop_constraint("chats_kind_check", "chats", type_="check")
    op.drop_column("chats", "created_at")
    op.drop_column("chats", "kind")
