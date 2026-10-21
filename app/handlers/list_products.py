"""The /list command: product list, product card, removal.

The handlers only parse input, call ``TrackingService`` and render replies.
Ids from callbacks are user input: the typed ``CallbackData`` range-checks
them, and every service call is scoped to the Telegram user who pressed the
button. Product names come from a marketplace (untrusted), so every message
carrying one is sent with ``parse_mode=None``. ``/list`` never touches FSM
state. Each callback handler answers the callback exactly once.
"""

from typing import assert_never

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import CB_LIST_PREFIXES
from app.bot.keyboards import CB_MENU_LIST
from app.bot.keyboards import MAX_LIST_BUTTONS
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import nav_keyboard
from app.bot.keyboards import product_card_keyboard
from app.bot.keyboards import product_list_keyboard
from app.domain.links import build_product_url
from app.handlers.add_product import TrackingServiceMiddleware
from app.handlers.callback_reply import edit_callback_message
from app.services.tracking import AlertRemoval
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService


async def _list_screen(
    tracking: TrackingService, user_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of the list screen, shared by /list and the menu."""
    products = await tracking.list_products(user_id)
    keyboard = product_list_keyboard(products)
    if not products:
        return texts.LIST_EMPTY_TEXT, keyboard
    shown = min(len(products), MAX_LIST_BUTTONS)
    return texts.list_text(shown, len(products)), keyboard


def _card_screen(card: ProductCard) -> tuple[str, InlineKeyboardMarkup]:
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


def create_router() -> Router:
    """Build a fresh ``list_products`` router (see ``common.create_router``)."""
    router = Router(name="list_products")
    middleware = TrackingServiceMiddleware()
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)

    @router.message(Command("list"))
    async def cmd_list(message: Message, tracking: TrackingService) -> None:
        """Show the caller's products; leaves the FSM state alone."""
        if message.from_user is None:
            return
        text, keyboard = await _list_screen(tracking, message.from_user.id)
        await message.answer(text, reply_markup=keyboard, parse_mode=None)

    @router.callback_query(F.data == CB_MENU_LIST)
    async def on_menu_list(callback: CallbackQuery, tracking: TrackingService) -> None:
        """ "My products" button: the /list screen in place, for the presser."""
        text, keyboard = await _list_screen(tracking, callback.from_user.id)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.callback_query(ProductCardCb.filter())
    async def on_open_card(
        callback: CallbackQuery,
        callback_data: ProductCardCb,
        tracking: TrackingService,
    ) -> None:
        """Show the product card in place of the list."""
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)
            return
        text, keyboard = _card_screen(card)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(AlertRemoveCb.filter())
    async def on_remove_alert(
        callback: CallbackQuery,
        callback_data: AlertRemoveCb,
        tracking: TrackingService,
    ) -> None:
        """Remove one threshold of the pressing user and redraw the card.

        ``product_id`` is user input too: the card is read scoped to the
        pressing user, so a forged id yields no card, never someone's data.
        """
        user_id = callback.from_user.id
        outcome = await tracking.remove_alert(user_id, callback_data.alert_id)
        if outcome is AlertRemoval.removed_with_product:
            await edit_callback_message(
                callback,
                texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
                reply_markup=nav_keyboard(),
            )
            return
        if outcome is AlertRemoval.removed:
            answer, gone_text = texts.ALERT_REMOVED_TEXT, texts.ALERT_REMOVED_TEXT
        elif outcome is AlertRemoval.not_found:
            answer = texts.ALERT_ALREADY_REMOVED_TEXT
            gone_text = texts.PRODUCT_ALREADY_GONE_TEXT
        else:
            assert_never(outcome)
        card = await tracking.get_product_card(user_id, callback_data.product_id)
        if card is None:
            await edit_callback_message(
                callback,
                gone_text,
                reply_markup=nav_keyboard(),
                alert_text=gone_text,
            )
            return
        text, keyboard = _card_screen(card)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=answer,
            answer_text=answer,
        )

    @router.callback_query(ProductRemoveCb.filter())
    async def on_remove_product(
        callback: CallbackQuery,
        callback_data: ProductRemoveCb,
        tracking: TrackingService,
    ) -> None:
        """Stop tracking one product of the pressing user."""
        removed = await tracking.remove_product(
            callback.from_user.id, callback_data.product_id
        )
        text = (
            texts.LIST_PRODUCT_REMOVED_TEXT
            if removed
            else texts.PRODUCT_ALREADY_GONE_TEXT
        )
        await edit_callback_message(
            callback, text, reply_markup=nav_keyboard(), alert_text=text
        )

    @router.callback_query(F.data.startswith(CB_LIST_PREFIXES))
    async def on_stale_list_button(callback: CallbackQuery) -> None:
        """Forged or malformed list payload: never touch the service."""
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)

    return router
