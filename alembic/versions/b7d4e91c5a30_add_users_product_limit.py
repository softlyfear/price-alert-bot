"""add users product_limit

Revision ID: b7d4e91c5a30
Revises: eafcd2321542
Create Date: 2026-10-22 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d4e91c5a30"
down_revision: str | Sequence[str] | None = "eafcd2321542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("product_limit", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_users_product_limit_positive",
        "users",
        "product_limit > 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_users_product_limit_positive", "users", type_="check")
    op.drop_column("users", "product_limit")
