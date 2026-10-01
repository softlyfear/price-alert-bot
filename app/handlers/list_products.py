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
from app.bot.keyboards import MAX_LIST_BUTTONS
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import product_card_keyboard
from app.bot.keyboards import product_list_keyboard
from app.domain.links import build_product_url
from app.handlers.add_product import TrackingServiceMiddleware
from app.services.tracking import AlertRemoval
from app.services.tracking import TrackingService


async def _reply(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    alert_text: str | None = None,
) -> None:
    """Answer the callback exactly once and deliver the outcome to the user.

    A message that still has a chat (accessible or not) gets a new message in
    that chat; with no message at all the outcome is shown as a popup alert
    (``alert_text`` if given, else ``text``).
    """
    message = callback.message
    if message is None:
        await callback.answer(alert_text or text, show_alert=True)
        return
    await callback.answer()
    await message.answer(text, reply_markup=reply_markup, parse_mode=None)


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
        products = await tracking.list_products(message.from_user.id)
        if not products:
            await message.answer(texts.LIST_EMPTY_TEXT)
            return
        shown = min(len(products), MAX_LIST_BUTTONS)
        await message.answer(
            texts.list_text(shown, len(products)),
            reply_markup=product_list_keyboard(products),
            parse_mode=None,
        )

    @router.callback_query(ProductCardCb.filter())
    async def on_open_card(
        callback: CallbackQuery,
        callback_data: ProductCardCb,
        tracking: TrackingService,
    ) -> None:
        """Show the product card in a new message."""
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)
            return
        product = card.product
        await _reply(
            callback,
            texts.product_card_text(
                product.product_name,
                product.marketplace,
                build_product_url(product.marketplace, product.article),
                product.current_price,
                [(alert.direction, alert.target_price) for alert in card.alerts],
            ),
            reply_markup=product_card_keyboard(product.id, card.alerts),
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(AlertRemoveCb.filter())
    async def on_remove_alert(
        callback: CallbackQuery,
        callback_data: AlertRemoveCb,
        tracking: TrackingService,
    ) -> None:
        """Remove one threshold of the pressing user."""
        outcome = await tracking.remove_alert(
            callback.from_user.id, callback_data.alert_id
        )
        match outcome:
            case AlertRemoval.not_found:
                await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)
            case AlertRemoval.removed:
                await _reply(callback, texts.LIST_ALERT_REMOVED_TEXT)
            case AlertRemoval.removed_with_product:
                await _reply(callback, texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT)
            case _:
                assert_never(outcome)

    @router.callback_query(ProductRemoveCb.filter())
    async def on_remove_product(
        callback: CallbackQuery,
        callback_data: ProductRemoveCb,
        tracking: TrackingService,
    ) -> None:
        """Stop tracking one product of the pressing user."""
        if await tracking.remove_product(
            callback.from_user.id, callback_data.product_id
        ):
            await _reply(callback, texts.LIST_PRODUCT_REMOVED_TEXT)
        else:
            await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)

    @router.callback_query(F.data.startswith(CB_LIST_PREFIXES))
    async def on_stale_list_button(callback: CallbackQuery) -> None:
        """Forged or malformed list payload: never touch the service."""
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)

    return router
