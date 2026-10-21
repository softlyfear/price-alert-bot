"""Shared builders of the PAB-096 handler tests (repeat link, add button, cancels).

The card here deliberately differs from the dialog stub's ``_NAME``/``_PRICE``:
a value that reaches the screen or the FSM from the database (the card) can
not be mistaken for one that came from the marketplace preview.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from aiogram.methods import EditMessageText
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.services.tracking import ProductCard

NB = "\u00a0"
DB_NAME = "Из базы <b>1"
DB_PRICE = 175_500  # 1 755 rubles
DB_ARTICLE = 12345678
DB_FETCHED_AT = datetime(2026, 10, 21, 11, 5, tzinfo=UTC)
PRODUCT_ID = 5
WB_LINK = f"https://www.wildberries.ru/catalog/{DB_ARTICLE}/detail.aspx"
MENU = ["menu:add", "menu:list", "menu:help"]
CARD_NAV = [("← К списку", "menu:list"), ("Главное меню", "menu:start")]
LEGEND = "↓ — сообщу, когда цена снизится до порога\n↑ — сообщу, когда вырастет до него"
EDIT_STATE = "EditAlert:waiting_target_price"
EDIT_DATA: dict[str, Any] = {"alert_id": 11, "product_id": PRODUCT_ID}
ADD_STATES = [
    "AddProduct:waiting_link",
    "AddProduct:waiting_target_price",
    "AddProduct:confirming",
]
ADD_DATA: dict[str, Any] = {
    "marketplace": "ozon",
    "article": 777,
    "product_name": "draft",
    "current_price": 999,
    "target_price": 500,
    "confirm_message_id": 4,
    "link_message_id": 3,
    "price_fetched_at": "2020-01-02T03:04:05+00:00",  # another product's stamp
}
CARD_TEXT = (
    f"{DB_NAME}\n"
    "Маркетплейс: Wildberries\n"
    f"https://www.wildberries.ru/catalog/{DB_ARTICLE}/detail.aspx\n"
    f"Текущая цена: 1{NB}755{NB}₽\n"
    "Цена без учёта персональных скидок.\n"
    "\n"
    "Пороги:\n"
    f"• ↓ 1{NB}500{NB}₽\n"
    "\n"
    f"{LEGEND}"
)


def product(
    product_id: int = PRODUCT_ID, price_fetched_at: datetime | None = DB_FETCHED_AT
) -> Product:
    return Product(
        id=product_id,
        user_id=1,
        marketplace=Marketplace.wb,
        article=DB_ARTICLE,
        product_name=DB_NAME,
        current_price=DB_PRICE,
        price_fetched_at=price_fetched_at,
    )


def card(price_fetched_at: datetime | None = DB_FETCHED_AT) -> ProductCard:
    """The tracked product with one ``below`` threshold of 1 500 rubles."""
    alert = Alert(
        id=11,
        user_id=1,
        product_id=PRODUCT_ID,
        target_price=150_000,
        direction=AlertDirection.below,
    )
    return ProductCard(
        product=product(price_fetched_at=price_fetched_at), alerts=(alert,)
    )


def rows(request: SendMessage | EditMessageText) -> list[list[tuple[str, str | None]]]:
    markup = request.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def callbacks(request: SendMessage | EditMessageText) -> list[str | None]:
    return [data for row in rows(request) for _, data in row]
