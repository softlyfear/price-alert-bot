"""Product model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.base import TimestampMixin
from app.models.base import UTCDateTime
from app.models.enums import Marketplace

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.user import User


class Product(Base, TimestampMixin):
    """Stores marketplace item metadata and tracks price history for a specific user."""

    __tablename__ = "products"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    marketplace: Mapped[Marketplace] = mapped_column(
        Enum(Marketplace, create_constraint=True, validate_strings=True)
    )
    article: Mapped[int] = mapped_column(BigInteger)
    wb_size_id: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    wb_size_name: Mapped[str | None] = mapped_column()
    product_name: Mapped[str] = mapped_column()
    current_price: Mapped[int] = mapped_column(
        CheckConstraint(
            "current_price >= 0", name="ck_products_current_price_non_negative"
        )
    )
    previous_price: Mapped[int | None] = mapped_column(
        CheckConstraint(
            "previous_price >= 0", name="ck_products_previous_price_non_negative"
        )
    )
    initial_price: Mapped[int | None] = mapped_column(
        CheckConstraint(
            "initial_price >= 0", name="ck_products_initial_price_non_negative"
        )
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    price_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "marketplace",
            "article",
            "wb_size_id",
            name="uq_user_marketplace_article_size",
        ),
        CheckConstraint(
            "typeof(wb_size_id) = 'integer' AND "
            "wb_size_id BETWEEN 0 AND 9223372036854775807",
            name="ck_products_wb_size_id_range",
        ),
        CheckConstraint(
            "wb_size_id = 0 OR marketplace = 'wb'",
            name="ck_products_wb_size_marketplace",
        ),
        CheckConstraint(
            "wb_size_id != 0 OR wb_size_name IS NULL", name="ck_products_wb_size_name"
        ),
        {"sqlite_autoincrement": True},
    )

    user: Mapped["User"] = relationship(
        "User",
        back_populates="products",
    )

    alerts: Mapped[list["Alert"]] = relationship(
        "Alert",
        back_populates="product",
        cascade="all, delete-orphan",
    )
