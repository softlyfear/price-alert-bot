"""Notification service."""

from datetime import UTC
from datetime import datetime
from decimal import Decimal

from aiogram import Bot
from loguru import logger

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.product import Product
from app.models.user import User


def _to_rub(value: int) -> str:
    return f"{(Decimal(value) / Decimal('100')):.2f}"


class NotificationService:
    """Notification service for telegram bot."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_alert(
        self,
        alert: Alert,
        product: Product,
        user: User,
    ) -> None:
        """Alert's notification logic for users."""

        arrow = "↘" if alert.direction == AlertDirection.below else "↗"
        direction_text = "ниже" if alert.direction == AlertDirection.below else "выше"
        wb_link = f"https://www.wildberries.ru/catalog/{product.article}/detail.aspx"

        message = (
            f"{arrow} Сработал алерт\n"
            f"Товар: {product.product_name}\n"
            f"Артикул: {product.article}\n"
            f"Текущая цена: {_to_rub(product.current_price)} ₽\n"
            f"Порог: {direction_text} {_to_rub(alert.target_price)} ₽\n"
            f"Ссылка: {wb_link}"
        )

        try:
            await self._bot.send_message(chat_id=user.tg_user_id, text=message)
        except Exception as exc:
            logger.exception(
                "Failed to send alert notification",
                alert_id=alert.id,
                product_id=product.id,
                user_id=user.id,
                tg_user_id=user.tg_user_id,
                error=str(exc),
            )
            raise

        alert.triggered_at = datetime.now(UTC)
