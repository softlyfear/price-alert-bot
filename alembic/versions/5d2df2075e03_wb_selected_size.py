"""Persist a selected Wildberries size.

Revision ID: 5d2df2075e03
Revises: 6cd914885374
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5d2df2075e03"
down_revision: str | Sequence[str] | None = "6cd914885374"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(*, selected_size: bool) -> None:
    connection = op.get_bind()
    sequence = connection.exec_driver_sql(
        "SELECT seq FROM sqlite_sequence WHERE name = 'products'"
    ).scalar_one_or_none()
    # Keep children outside the parent drop so ON DELETE CASCADE cannot erase them.
    op.execute("CREATE TEMP TABLE _pab126_alerts AS SELECT * FROM alerts")
    op.execute("DELETE FROM alerts")
    with op.batch_alter_table(
        "products", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch:
        if selected_size:
            batch.add_column(
                sa.Column(
                    "wb_size_id",
                    sa.BigInteger(),
                    server_default=sa.text("0"),
                    nullable=False,
                )
            )
            batch.add_column(sa.Column("wb_size_name", sa.String(), nullable=True))
            batch.drop_constraint("uq_user_marketplace_article", type_="unique")
            batch.create_unique_constraint(
                "uq_user_marketplace_article_size",
                ["user_id", "marketplace", "article", "wb_size_id"],
            )
            batch.create_check_constraint(
                "ck_products_wb_size_id_range",
                "typeof(wb_size_id) = 'integer' AND "
                "wb_size_id BETWEEN 0 AND 9223372036854775807",
            )
            batch.create_check_constraint(
                "ck_products_wb_size_marketplace",
                "wb_size_id = 0 OR marketplace = 'wb'",
            )
            batch.create_check_constraint(
                "ck_products_wb_size_name", "wb_size_id != 0 OR wb_size_name IS NULL"
            )
        else:
            batch.drop_constraint("ck_products_wb_size_id_range", type_="check")
            batch.drop_constraint("ck_products_wb_size_marketplace", type_="check")
            batch.drop_constraint("ck_products_wb_size_name", type_="check")
            batch.drop_constraint("uq_user_marketplace_article_size", type_="unique")
            batch.create_unique_constraint(
                "uq_user_marketplace_article", ["user_id", "marketplace", "article"]
            )
            batch.drop_column("wb_size_name")
            batch.drop_column("wb_size_id")
    if sequence is not None:
        connection.exec_driver_sql(
            "DELETE FROM sqlite_sequence WHERE name = 'products'"
        )
        connection.exec_driver_sql(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('products', ?)",
            (sequence,),
        )
    op.execute("INSERT INTO alerts SELECT * FROM _pab126_alerts")
    op.execute("DROP TABLE _pab126_alerts")


def upgrade() -> None:
    """Upgrade schema and preserve dependent alerts and issued product IDs."""
    _rebuild(selected_size=True)


def downgrade() -> None:
    """Refuse to discard selected sizes, then restore the legacy schema."""
    if (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM products WHERE wb_size_id != 0 LIMIT 1")
        .first()
    ):
        raise RuntimeError("Cannot downgrade while selected WB sizes exist")
    _rebuild(selected_size=False)
