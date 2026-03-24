from sqlalchemy import BigInteger
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.models import Base
from app.models import TimestampMixin


class User(Base, TimestampMixin):
    """Telegram user with his tg_id and username."""

    __tablename__ = "users"

    tg_user_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
    )
    tg_username: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
    )
