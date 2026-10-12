"""utc now defaults and price checks

Revision ID: a1f3c9d27b64
Revises: c55fadc558a3
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f3c9d27b64"
down_revision: str | Sequence[str] | None = "c55fadc558a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP_TABLES = ("users", "products", "alerts")
_TIMESTAMP_COLUMNS = ("created_at", "updated_at")

_CHECKS = (
    ("products", "ck_products_current_price_non_negative", "current_price >= 0"),
    ("products", "ck_products_previous_price_non_negative", "previous_price >= 0"),
    ("alerts", "ck_alerts_target_price_non_negative", "target_price >= 0"),
)


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TIMESTAMP_TABLES:
        for column in _TIMESTAMP_COLUMNS:
            op.alter_column(table, column, server_default=sa.text("now()"))
    for table, name, condition in _CHECKS:
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    """Downgrade schema."""
    for table, name, _ in reversed(_CHECKS):
        op.drop_constraint(name, table, type_="check")
    for table in _TIMESTAMP_TABLES:
        for column in _TIMESTAMP_COLUMNS:
            op.alter_column(
                table, column, server_default=sa.text("TIMEZONE('utc', now())")
            )
