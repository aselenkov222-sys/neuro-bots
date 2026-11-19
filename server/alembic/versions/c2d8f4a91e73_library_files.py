"""library files

Revision ID: c2d8f4a91e73
Revises: e7a1c4b82d60
Create Date: 2026-11-19 11:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2d8f4a91e73"
down_revision: Union[str, Sequence[str], None] = "e7a1c4b82d60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NAME_CHECK = (
    "name LIKE '%.xlsx' AND strpos(name, '/') = 0 "
    "AND strpos(name, chr(92)) = 0 AND char_length(name) >= 5"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "library_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_NAME_CHECK, name="library_files_name_check"),
        sa.ForeignKeyConstraint(
            ["bot_id"], ["bots.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["messages.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("library_files")
