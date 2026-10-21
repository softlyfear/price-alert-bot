"""Product card screen shared by the /list and /add routers.

Lives in its own module so that both routers can import it without an
import cycle.
"""

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.keyboards import product_card_keyboard
from app.domain.links import build_product_url
from app.services.tracking import ProductCard


def card_screen(card: ProductCard) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of the product card screen."""
    product = card.product
    text = texts.product_card_text(
        product.product_name,
        product.marketplace,
        build_product_url(product.marketplace, product.article),
        product.current_price,
        [(alert.direction, alert.target_price) for alert in card.alerts],
    )
    return text, product_card_keyboard(product.id, card.alerts)
