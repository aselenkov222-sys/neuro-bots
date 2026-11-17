"""email confirmed at

Revision ID: 048ee727aa3e
Revises: ff582d8bc7fb
Create Date: 2026-11-17 11:38:41.989866

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '048ee727aa3e'
down_revision: Union[str, Sequence[str], None] = 'ff582d8bc7fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "accounts",
        sa.Column(
            "email_confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("accounts", "email_confirmed_at")
