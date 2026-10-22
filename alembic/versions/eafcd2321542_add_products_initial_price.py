"""add products initial_price

Revision ID: eafcd2321542
Revises: a06e7f4948b5
Create Date: 2026-10-22 12:05:24.300167

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eafcd2321542"
down_revision: str | Sequence[str] | None = "a06e7f4948b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("products", sa.Column("initial_price", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_products_initial_price_non_negative",
        "products",
        "initial_price >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "ck_products_initial_price_non_negative", "products", type_="check"
    )
    op.drop_column("products", "initial_price")
