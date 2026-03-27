from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.base import TimestampMixin
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
    target_price: Mapped[int] = mapped_column(CheckConstraint("target_price >= 0"))
    direction: Mapped[AlertDirection] = mapped_column(Enum(AlertDirection))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "product_id",
            "direction",
            "target_price",
            name="uq_alerts_user_product_logic",
        ),
    )

    product: Mapped["Product"] = relationship(
        "Product",
        back_populates="alerts",
    )

    user: Mapped["User"] = relationship(
        "User",
        back_populates="alerts",
    )
