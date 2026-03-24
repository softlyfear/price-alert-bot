from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.base import TimestampMixin
from app.models.enums import Marketplace

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.user import User


class Product(Base, TimestampMixin):
    """Stores marketplace item metadata and tracks price history for a specific user."""

    __tablename__ = "products"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    marketplace: Mapped[Marketplace] = mapped_column(Enum(Marketplace))
    article: Mapped[int] = mapped_column(BigInteger)
    product_name: Mapped[str] = mapped_column()
    current_price: Mapped[int] = mapped_column()
    previous_price: Mapped[int | None] = mapped_column()
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("TIMEZONE('utc', now())")
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
