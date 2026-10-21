"""add products price_fetched_at

Revision ID: a06e7f4948b5
Revises: a1f3c9d27b64
Create Date: 2026-10-22 00:39:18.665382

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a06e7f4948b5"
down_revision: str | Sequence[str] | None = "a1f3c9d27b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "products",
        sa.Column("price_fetched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("products", "price_fetched_at")
