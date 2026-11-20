"""inbound webhook secret

Revision ID: c7a1e9d24b83
Revises: b6d2e8a14c70
Create Date: 2026-11-20 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from server.bots import new_inbound_secret


# revision identifiers, used by Alembic.
revision: str = "c7a1e9d24b83"
down_revision: Union[str, Sequence[str], None] = "b6d2e8a14c70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("bots", sa.Column("inbound_secret", sa.Text(), nullable=True))
    connection = op.get_bind()
    bot_ids = connection.execute(sa.text("SELECT id FROM bots")).scalars().all()
    for bot_id in bot_ids:
        connection.execute(
            sa.text("UPDATE bots SET inbound_secret = :secret WHERE id = :id"),
            {"secret": new_inbound_secret(), "id": bot_id},
        )
    op.alter_column("bots", "inbound_secret", nullable=False)
    op.create_unique_constraint(
        "bots_inbound_secret_key", "bots", ["inbound_secret"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("bots_inbound_secret_key", "bots", type_="unique")
    op.drop_column("bots", "inbound_secret")
