"""The /list command: product list, product card, removal.

The handlers only parse input, call ``TrackingService`` and render replies.
Ids from callbacks are user input: the typed ``CallbackData`` range-checks
them, and every service call is scoped to the Telegram user who pressed the
button. Product names come from a marketplace (untrusted), so every message
carrying one is sent with ``parse_mode=None``. ``/list`` never touches FSM
state, except the one-step ``EditAlert`` dialog that changes a threshold.
Each callback handler answers the callback exactly once.
"""

from typing import assert_never

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import CB_LIST_PREFIXES
from app.bot.keyboards import CB_MENU_LIST
from app.bot.keyboards import MAX_LIST_BUTTONS
from app.bot.keyboards import AlertAddCb
from app.bot.keyboards import AlertEditCb
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import EditCancelCb
from app.bot.keyboards import NoticeCardCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import edit_cancel_keyboard
from app.bot.keyboards import main_menu_keyboard
from app.bot.keyboards import nav_keyboard
from app.bot.keyboards import product_list_keyboard
from app.bot.states import AddProduct
from app.bot.states import EditAlert
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import InvalidPriceInputError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.domain.money import rubles_to_kopecks
from app.handlers.add_product import MAX_TEXT_LENGTH
from app.handlers.add_product import TrackingServiceMiddleware
from app.handlers.add_product import enter_price_step
from app.handlers.callback_reply import edit_callback_message
from app.handlers.callback_reply import reply_to_callback
from app.handlers.product_card import card_screen
from app.services.tracking import AlertChange
from app.services.tracking import AlertRemoval
from app.services.tracking import TrackingService

_KEY_ALERT_ID = "alert_id"
_KEY_PRODUCT_ID = "product_id"


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
        text, keyboard = card_screen(card)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(NoticeCardCb.filter())
    async def on_notice_open_card(
        callback: CallbackQuery,
        callback_data: NoticeCardCb,
        tracking: TrackingService,
    ) -> None:
        """Notification "Open product": the card as a new message (PROJECT.md Р10).

        ``product_id`` is user input: the card is read for the pressing user.
        """
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)
            return
        text, keyboard = card_screen(card)
        await reply_to_callback(
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
        text, keyboard = card_screen(card)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=answer,
            answer_text=answer,
        )

    @router.callback_query(AlertEditCb.filter())
    async def on_edit_alert(
        callback: CallbackQuery,
        callback_data: AlertEditCb,
        state: FSMContext,
        tracking: TrackingService,
    ) -> None:
        """Start the one-step threshold edit for the pressing user's alert.

        The card is read scoped to the pressing user, so a forged id yields
        no card and no state. An explicit press replaces any running dialog.
        """
        if callback.message is None:
            await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)
            return
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await edit_callback_message(
                callback,
                texts.PRODUCT_ALREADY_GONE_TEXT,
                reply_markup=nav_keyboard(),
                alert_text=texts.PRODUCT_ALREADY_GONE_TEXT,
            )
            return
        alert = next((a for a in card.alerts if a.id == callback_data.alert_id), None)
        if alert is None:
            text, keyboard = card_screen(card)
            await edit_callback_message(
                callback,
                text,
                reply_markup=keyboard,
                alert_text=texts.ALERT_ALREADY_REMOVED_TEXT,
                answer_text=texts.ALERT_ALREADY_REMOVED_TEXT,
            )
            return
        await state.clear()
        await state.set_state(EditAlert.waiting_target_price)
        await state.update_data(
            {_KEY_ALERT_ID: alert.id, _KEY_PRODUCT_ID: card.product.id}
        )
        await edit_callback_message(
            callback,
            texts.edit_prompt_text(
                card.product.product_name,
                card.product.current_price,
                alert.direction,
                alert.target_price,
            ),
            reply_markup=edit_cancel_keyboard(card.product.id),
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(AlertAddCb.filter())
    async def on_add_alert(
        callback: CallbackQuery,
        callback_data: AlertAddCb,
        state: FSMContext,
        tracking: TrackingService,
    ) -> None:
        """Start the threshold step of /add for a product of the pressing user.

        The product and its price come from the database, scoped to the
        pressing user; nothing is written and no marketplace is asked. An
        explicit press replaces any running dialog.
        """
        if callback.message is None:
            await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)
            return
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await edit_callback_message(
                callback,
                texts.PRODUCT_ALREADY_GONE_TEXT,
                reply_markup=nav_keyboard(),
                alert_text=texts.PRODUCT_ALREADY_GONE_TEXT,
            )
            return
        product = card.product
        await state.clear()
        await enter_price_step(
            state,
            product.marketplace,
            product.article,
            product.product_name,
            product.current_price,
        )
        await edit_callback_message(
            callback,
            texts.product_found_text(product.product_name, product.current_price),
            reply_markup=cancel_keyboard(),
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.message(
        StateFilter(EditAlert.waiting_target_price), ~Command("add"), F.text
    )
    async def on_edit_price(
        message: Message, state: FSMContext, tracking: TrackingService
    ) -> None:
        """Convert the typed rubles and change the threshold.

        ``/add`` is left to its own handler so it can restart the dialog.
        """
        if message.from_user is None or message.text is None:
            return
        user_id = message.from_user.id
        data = await state.get_data()
        alert_id = data.get(_KEY_ALERT_ID)
        product_id = data.get(_KEY_PRODUCT_ID)
        if not isinstance(alert_id, int) or not isinstance(product_id, int):
            await state.clear()
            await message.answer(
                texts.EDIT_STALE_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        try:
            target_price = rubles_to_kopecks(message.text[:MAX_TEXT_LENGTH])
        except InvalidPriceInputError:
            await message.answer(
                texts.ADD_PRICE_INVALID_TEXT,
                reply_markup=edit_cancel_keyboard(product_id),
            )
            return
        try:
            outcome = await tracking.change_alert_target(
                user_id, alert_id, target_price
            )
        except TargetEqualsCurrentPriceError:
            await message.answer(
                texts.ADD_TARGET_EQUALS_TEXT,
                reply_markup=edit_cancel_keyboard(product_id),
            )
            return
        except DuplicateAlertError:
            await message.answer(
                texts.EDIT_DUPLICATE_TEXT,
                reply_markup=edit_cancel_keyboard(product_id),
            )
            return
        await state.clear()
        card = await tracking.get_product_card(user_id, product_id)
        if card is None:
            await message.answer(
                texts.PRODUCT_ALREADY_GONE_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        if outcome is AlertChange.changed:
            changed = next((a for a in card.alerts if a.id == alert_id), None)
            summary = texts.edit_changed_text(
                changed.direction if changed is not None else None, target_price
            )
        elif outcome is AlertChange.unchanged:
            summary = texts.EDIT_UNCHANGED_TEXT
        elif outcome is AlertChange.not_found:
            summary = texts.ALERT_ALREADY_REMOVED_TEXT
        else:
            assert_never(outcome)
        card_text, keyboard = card_screen(card)
        await message.answer(
            f"{summary}\n\n{card_text}", reply_markup=keyboard, parse_mode=None
        )

    @router.message(StateFilter(EditAlert.waiting_target_price), ~F.text)
    async def on_edit_not_text(message: Message, state: FSMContext) -> None:
        """Non-text input on the edit step: explain, keep the state."""
        product_id = (await state.get_data()).get(_KEY_PRODUCT_ID)
        if not isinstance(product_id, int):
            await state.clear()
            await message.answer(
                texts.EDIT_STALE_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        await message.answer(
            texts.EDIT_NOT_TEXT_TEXT, reply_markup=edit_cancel_keyboard(product_id)
        )

    @router.callback_query(EditCancelCb.filter(), StateFilter(AddProduct))
    async def on_stale_edit_cancel(callback: CallbackQuery) -> None:
        """Old edit "Cancel" must not end an /add dialog."""
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)

    @router.callback_query(EditCancelCb.filter())
    async def on_edit_cancel(
        callback: CallbackQuery,
        callback_data: EditCancelCb,
        state: FSMContext,
        tracking: TrackingService,
    ) -> None:
        """Leave the edit step and put the product card back in place.

        A "Cancel" of another product's edit dialog is stale: it changes nothing.
        """
        data = await state.get_data()
        product_id = data.get(_KEY_PRODUCT_ID)
        if (
            await state.get_state() == EditAlert.waiting_target_price.state
            and isinstance(product_id, int)
            and product_id != callback_data.product_id
        ):
            await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)
            return
        await state.clear()
        card = (
            await tracking.get_product_card(callback.from_user.id, product_id)
            if isinstance(product_id, int)
            else None
        )
        if card is None:
            await edit_callback_message(
                callback,
                texts.CANCEL_TEXT,
                reply_markup=main_menu_keyboard(),
                alert_text=texts.CANCEL_TEXT,
            )
            return
        text, keyboard = card_screen(card)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
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
