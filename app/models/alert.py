"""Alert model."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
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
from app.models.enums import AlertDirection

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class Alert(Base, TimestampMixin):
    """Defines a price threshold trigger and notification status for a product."""

    __tablename__ = "alerts"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    target_price: Mapped[int] = mapped_column(
        CheckConstraint("target_price >= 0", name="ck_alerts_target_price_non_negative")
    )
    direction: Mapped[AlertDirection] = mapped_column(
        Enum(AlertDirection, create_constraint=True, validate_strings=True)
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean(create_constraint=True, name="ck_alerts_is_active_boolean"),
        server_default=text("true"),
    )
    triggered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "product_id",
            "direction",
            "target_price",
            name="uq_alerts_user_product_logic",
        ),
        {"sqlite_autoincrement": True},
    )

    product: Mapped["Product"] = relationship(
        "Product",
        back_populates="alerts",
    )

    user: Mapped["User"] = relationship(
        "User",
        back_populates="alerts",
    )
