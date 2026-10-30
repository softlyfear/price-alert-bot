"""User model."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import CheckConstraint
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.product import Product


class User(Base, TimestampMixin):
    """Represents a Telegram user for tracked products and alerts."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "length(tg_username) <= 32", name="ck_users_tg_username_length"
        ),
        {"sqlite_autoincrement": True},
    )

    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(32), unique=True)
    product_limit: Mapped[int | None] = mapped_column(
        CheckConstraint("product_limit > 0", name="ck_users_product_limit_positive")
    )

    products: Mapped[list["Product"]] = relationship(
        "Product",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    alerts: Mapped[list["Alert"]] = relationship(
        "Alert",
        back_populates="user",
        cascade="all, delete-orphan",
    )
