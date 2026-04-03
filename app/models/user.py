"""User model."""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
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

    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(32), unique=True)

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
