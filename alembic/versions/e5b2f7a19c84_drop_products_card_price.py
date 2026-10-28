"""drop products card_price

Revision ID: e5b2f7a19c84
Revises: d3a8c61f0b27
Create Date: 2026-10-28 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b2f7a19c84"
down_revision: str | Sequence[str] | None = "d3a8c61f0b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("ck_products_card_price_non_negative", "products", type_="check")
    op.drop_column("products", "card_price")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("products", sa.Column("card_price", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_products_card_price_non_negative",
        "products",
        "card_price >= 0",
    )
