"""Notification service."""

from datetime import UTC
from datetime import datetime
from typing import assert_never

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from loguru import logger

from app.domain.exceptions import DomainError
from app.domain.links import build_product_url
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.domain.money import format_price
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.product import Product
from app.models.user import User


class AlertDeliveryError(DomainError):
    """Raised when an alert notification could not be delivered to Telegram.

    The text is fixed on purpose: Telegram exception messages can carry the
    bot token or API URL, so the original error is neither chained nor logged.
    """

    def __init__(self) -> None:
        super().__init__("Alert notification delivery failed")


def _headline(direction: AlertDirection) -> str:
    match direction:
        case AlertDirection.below:
            return "↘ Цена снизилась"
        case AlertDirection.above:
            return "↗ Цена выросла"
        case _:
            assert_never(direction)


def _threshold_label(direction: AlertDirection) -> str:
    match direction:
        case AlertDirection.below:
            return "Порог: не выше"
        case AlertDirection.above:
            return "Порог: не ниже"
        case _:
            assert_never(direction)


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
        """Send the alert notification and mark the alert triggered on delivery.

        `triggered_at` is set only after Telegram accepted the message. A user
        who blocked the bot is logged and skipped; any other failure raises
        `AlertDeliveryError` without the original exception text.
        """

        message = (
            f"{_headline(alert.direction)}\n"
            f"Товар: {product.product_name}\n"
            f"Артикул: {product.article}\n"
            f"Текущая цена: {format_price(product.current_price)}\n"
            f"{_threshold_label(alert.direction)} {format_price(alert.target_price)}\n"
            f"{PRICE_DISCLAIMER_SHORT}.\n"
            f"Ссылка: {build_product_url(product.marketplace, product.article)}"
        )
        log_context = logger.bind(
            alert_id=alert.id,
            product_id=product.id,
            user_id=user.id,
        )

        try:
            await self._bot.send_message(
                chat_id=user.tg_user_id, text=message, parse_mode=None
            )
        except TelegramForbiddenError:
            log_context.warning("Alert not delivered: user blocked the bot")
            return
        except Exception as exc:
            log_context.error(
                "Failed to send alert notification",
                error_type=type(exc).__name__,
            )
            raise AlertDeliveryError from None

        alert.triggered_at = datetime.now(UTC)
        log_context.info("Alert notification delivered")
