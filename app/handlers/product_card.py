"""Product card screen shared by the /list and /add routers.

Lives in its own module so that both routers can import it without an
import cycle.
"""

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import product_card_keyboard
from app.domain.links import build_product_url
from app.services.tracking import ProductCard


def card_screen(
    card: ProductCard, display_timezone: ZoneInfo
) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of the product card screen."""
    product = card.product
    fetched_at = product.price_fetched_at
    price_label = (
        texts.price_time_label(fetched_at, datetime.now(UTC), display_timezone)
        if fetched_at is not None
        else None
    )
    initial_line = (
        texts.initial_price_text(
            product.initial_price,
            product.current_price,
            texts.added_date_label(
                product.created_at, datetime.now(UTC), display_timezone
            ),
        )
        if product.initial_price is not None
        else None
    )
    text = texts.product_card_text(
        product.product_name,
        product.marketplace,
        build_product_url(product.marketplace, product.article),
        product.current_price,
        [(alert.direction, alert.target_price) for alert in card.alerts],
        price_label,
        initial_line=initial_line,
    )
    return text, product_card_keyboard(product.id, card.alerts)
