"""add products card_price

Revision ID: d3a8c61f0b27
Revises: b7d4e91c5a30
Create Date: 2026-10-23 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a8c61f0b27"
down_revision: str | Sequence[str] | None = "b7d4e91c5a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("card_price", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_products_card_price_non_negative",
        "products",
        "card_price >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_products_card_price_non_negative", "products", type_="check")
    op.drop_column("products", "card_price")
