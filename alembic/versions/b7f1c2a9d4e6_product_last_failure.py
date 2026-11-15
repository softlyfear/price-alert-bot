"""Persist the last fetch failure of a product.

Revision ID: b7f1c2a9d4e6
Revises: 5d2df2075e03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7f1c2a9d4e6"
down_revision: str | Sequence[str] | None = "5d2df2075e03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = (
    "last_failure IS NULL OR last_failure IN "
    "('not_found', 'blocked', 'transport_error', 'bad_payload', 'out_of_stock')"
)


def upgrade() -> None:
    """Add the nullable status column; SQLite adds the named check in place.

    A table rebuild is avoided on purpose: products is referenced by alerts
    with ON DELETE CASCADE, and the column is nullable, so ADD COLUMN is both
    sufficient and non-destructive.
    """
    op.execute(
        "ALTER TABLE products ADD COLUMN last_failure VARCHAR(15) "
        f"CONSTRAINT ck_products_last_failure CHECK ({_CHECK})"
    )


def downgrade() -> None:
    """Drop the derived status column; products, alerts and ids are preserved."""
    op.drop_column("products", "last_failure")
