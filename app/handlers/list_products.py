"""The /list command: product list, product card, removal.

The handlers only parse input, call ``TrackingService`` and render replies.
Ids from callbacks are user input: the typed ``CallbackData`` range-checks
them, and every service call is scoped to the Telegram user who pressed the
button. Product names come from a marketplace (untrusted), so every message
carrying one is sent with ``parse_mode=None``. ``/list`` never touches FSM
state, except the one-step ``EditAlert`` dialog that changes a threshold.
Each callback handler answers the callback exactly once.
"""

from datetime import UTC
from datetime import datetime
from typing import assert_never
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from loguru import logger

from app.bot import texts
from app.bot.keyboards import CB_DLG_LIST
from app.bot.keyboards import CB_LIST_PREFIXES
from app.bot.keyboards import CB_LIST_REFRESH
from app.bot.keyboards import CB_MENU_LIST
from app.bot.keyboards import AlertAddCb
from app.bot.keyboards import AlertEditCb
from app.bot.keyboards import AlertRemoveCb
from app.bot.keyboards import EditCancelCb
from app.bot.keyboards import ListPageCb
from app.bot.keyboards import ListRefreshCb
from app.bot.keyboards import NoticeCardCb
from app.bot.keyboards import ProductCardCb
from app.bot.keyboards import ProductRefreshCb
from app.bot.keyboards import ProductRemoveCb
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import duplicate_exit_keyboard
from app.bot.keyboards import edit_cancel_keyboard
from app.bot.keyboards import main_menu_keyboard
from app.bot.keyboards import nav_keyboard
from app.bot.keyboards import no_buttons_keyboard
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
from app.handlers.callback_reply import edit_message_or_answer
from app.handlers.callback_reply import reply_to_callback
from app.handlers.product_card import card_screen
from app.services.price_service import PriceRefresher
from app.services.price_service import RefreshGone
from app.services.price_service import RefreshThrottled
from app.services.tracking import AlertChange
from app.services.tracking import AlertRemoval
from app.services.tracking import TrackingService

_KEY_ALERT_ID = "alert_id"
_KEY_PRODUCT_ID = "product_id"


async def _list_screen(
    tracking: TrackingService,
    user_id: int,
    display_timezone: ZoneInfo,
    page: int = 1,
) -> tuple[str, InlineKeyboardMarkup]:
    """Text and keyboard of one list page, shared by /list, the menu and paging.

    ``page`` is user input; the service clamps it to the existing pages.
    """
    product_page = await tracking.list_page(user_id, page)
    keyboard = product_list_keyboard(product_page)
    if not product_page.products:
        return texts.LIST_EMPTY_TEXT, keyboard
    stamps = [product.price_fetched_at for product in product_page.products]
    price_label = (
        texts.price_time_label(
            min(stamp for stamp in stamps if stamp is not None),
            datetime.now(UTC),
            display_timezone,
        )
        if all(stamp is not None for stamp in stamps)
        else None
    )
    return (
        texts.list_text(
            product_page.total, product_page.page, product_page.pages, price_label
        ),
        keyboard,
    )


async def _begin_refresh(
    callback: CallbackQuery, price_refresher: PriceRefresher
) -> Message | None:
    """Spend the refresh budget and show the progress screen.

    Returns ``None`` when nothing must run (stale button or spent budget, the
    popup is already shown); otherwise the message to edit with the result.
    The budget is checked before the callback is answered, so the refusal can
    be a popup.
    """
    if callback.message is None:
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)
        return None
    throttled = price_refresher.reserve(callback.from_user.id)
    if isinstance(throttled, RefreshThrottled):
        await callback.answer(
            texts.refresh_throttled_text(throttled.retry_after_seconds),
            show_alert=True,
        )
        return None
    return await edit_callback_message(
        callback, texts.REFRESH_IN_PROGRESS_TEXT, reply_markup=no_buttons_keyboard()
    )


async def _fail_refresh(progress: Message, user_id: int, error: Exception) -> None:
    """Replace the progress screen with a generic error; the error is logged.

    The exception is not re-raised: the ``errors`` handler would send a
    second copy of the message. Only the error type and ``user_id`` are bound.
    """
    logger.bind(user_id=user_id, error_type=type(error).__name__).exception(
        "Refresh failed after the progress screen"
    )
    await edit_message_or_answer(
        progress, texts.UNEXPECTED_ERROR_TEXT, reply_markup=nav_keyboard()
    )


def create_router() -> Router:
    """Build a fresh ``list_products`` router (see ``common.create_router``)."""
    router = Router(name="list_products")
    middleware = TrackingServiceMiddleware()
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)

    @router.message(Command("list"))
    async def cmd_list(
        message: Message, tracking: TrackingService, display_timezone: ZoneInfo
    ) -> None:
        """Show the caller's products; leaves the FSM state alone."""
        if message.from_user is None:
            return
        text, keyboard = await _list_screen(
            tracking, message.from_user.id, display_timezone
        )
        await message.answer(text, reply_markup=keyboard, parse_mode=None)

    @router.callback_query(F.data == CB_MENU_LIST)
    async def on_menu_list(
        callback: CallbackQuery, tracking: TrackingService, display_timezone: ZoneInfo
    ) -> None:
        """ "My products" button: the /list screen in place, for the presser."""
        text, keyboard = await _list_screen(
            tracking, callback.from_user.id, display_timezone
        )
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
        display_timezone: ZoneInfo,
    ) -> None:
        """Show the product card in place of the list."""
        card = await tracking.get_product_card(
            callback.from_user.id, callback_data.product_id
        )
        if card is None:
            await callback.answer(texts.LIST_GONE_ALERT_TEXT, show_alert=True)
            return
        text, keyboard = card_screen(card, display_timezone)
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
        display_timezone: ZoneInfo,
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
        text, keyboard = card_screen(card, display_timezone)
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
        display_timezone: ZoneInfo,
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
        text, keyboard = card_screen(card, display_timezone)
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
        display_timezone: ZoneInfo,
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
            text, keyboard = card_screen(card, display_timezone)
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
            price_fetched_at=product.price_fetched_at,
        )
        await edit_callback_message(
            callback,
            texts.product_found_text(product.product_name, product.current_price),
            reply_markup=cancel_keyboard(),
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(F.data == CB_DLG_LIST)
    async def on_dialog_list(
        callback: CallbackQuery,
        state: FSMContext,
        tracking: TrackingService,
        display_timezone: ZoneInfo,
    ) -> None:
        """Leave a dialog to the presser's list: clear the FSM, screen in place."""
        await state.clear()
        text, keyboard = await _list_screen(
            tracking, callback.from_user.id, display_timezone
        )
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.message(
        StateFilter(EditAlert.waiting_target_price), ~Command("add"), F.text
    )
    async def on_edit_price(
        message: Message,
        state: FSMContext,
        tracking: TrackingService,
        display_timezone: ZoneInfo,
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
                texts.ALERT_DUPLICATE_TEXT, reply_markup=duplicate_exit_keyboard()
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
        card_text, keyboard = card_screen(card, display_timezone)
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
        display_timezone: ZoneInfo,
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
        text, keyboard = card_screen(card, display_timezone)
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    async def refresh_list_page(
        callback: CallbackQuery,
        page: int,
        tracking: TrackingService,
        price_refresher: PriceRefresher,
        display_timezone: ZoneInfo,
    ) -> None:
        """Refresh all prices of the pressing user and redraw list page ``page``."""
        user_id = callback.from_user.id
        progress = await _begin_refresh(callback, price_refresher)
        if progress is None:
            return
        try:
            report = await price_refresher.refresh_list(user_id)
            text, keyboard = await _list_screen(
                tracking, user_id, display_timezone, page
            )
        except Exception as error:
            await _fail_refresh(progress, user_id, error)
            return
        if report.total:
            text = f"{texts.refresh_list_summary(report)}\n\n{text}"
        await edit_message_or_answer(progress, text, reply_markup=keyboard)

    @router.callback_query(F.data == CB_LIST_REFRESH)
    async def on_refresh_list(
        callback: CallbackQuery,
        tracking: TrackingService,
        price_refresher: PriceRefresher,
        display_timezone: ZoneInfo,
    ) -> None:
        """Old page-less "Refresh prices" button: stays on page 1."""
        await refresh_list_page(
            callback, 1, tracking, price_refresher, display_timezone
        )

    @router.callback_query(ListRefreshCb.filter())
    async def on_refresh_list_page(
        callback: CallbackQuery,
        callback_data: ListRefreshCb,
        tracking: TrackingService,
        price_refresher: PriceRefresher,
        display_timezone: ZoneInfo,
    ) -> None:
        """Refresh prices and stay on the page the button was pressed on."""
        await refresh_list_page(
            callback, callback_data.page, tracking, price_refresher, display_timezone
        )

    @router.callback_query(ListPageCb.filter())
    async def on_list_page(
        callback: CallbackQuery,
        callback_data: ListPageCb,
        tracking: TrackingService,
        display_timezone: ZoneInfo,
    ) -> None:
        """Turn the list page in place; the FSM is not touched."""
        text, keyboard = await _list_screen(
            tracking, callback.from_user.id, display_timezone, callback_data.page
        )
        await edit_callback_message(
            callback,
            text,
            reply_markup=keyboard,
            alert_text=texts.LIST_STALE_BUTTON_ALERT_TEXT,
        )

    @router.callback_query(ProductRefreshCb.filter())
    async def on_refresh_card(
        callback: CallbackQuery,
        callback_data: ProductRefreshCb,
        tracking: TrackingService,
        price_refresher: PriceRefresher,
        display_timezone: ZoneInfo,
    ) -> None:
        """Refresh the price of one product and redraw its card.

        ``product_id`` is user input: both the refresh and the card read are
        scoped to the pressing user in SQL.
        """
        user_id = callback.from_user.id
        progress = await _begin_refresh(callback, price_refresher)
        if progress is None:
            return
        try:
            result = await price_refresher.refresh_product(
                user_id, callback_data.product_id
            )
            card = (
                None
                if isinstance(result, RefreshGone)
                else await tracking.get_product_card(user_id, callback_data.product_id)
            )
            if isinstance(result, RefreshGone) or card is None:
                text, keyboard = await _list_screen(
                    tracking,
                    user_id,
                    display_timezone,
                    await tracking.list_page_of(user_id, callback_data.product_id),
                )
                await edit_message_or_answer(
                    progress,
                    f"{texts.REFRESH_GONE_TEXT}\n\n{text}",
                    reply_markup=keyboard,
                )
                return
        except Exception as error:
            await _fail_refresh(progress, user_id, error)
            return
        card_text, keyboard = card_screen(card, display_timezone)
        summary = texts.refresh_card_summary(result, card.product.marketplace)
        await edit_message_or_answer(
            progress, f"{summary}\n\n{card_text}", reply_markup=keyboard
        )

    @router.callback_query(ProductRemoveCb.filter())
    async def on_remove_product(
        callback: CallbackQuery,
        callback_data: ProductRemoveCb,
        tracking: TrackingService,
        display_timezone: ZoneInfo,
    ) -> None:
        """Stop tracking one product of the pressing user.

        Shows the list page the product was on (clamped if it is now gone).
        """
        user_id = callback.from_user.id
        removed = await tracking.remove_product(user_id, callback_data.product_id)
        text = (
            texts.LIST_PRODUCT_REMOVED_TEXT
            if removed
            else texts.PRODUCT_ALREADY_GONE_TEXT
        )
        list_text, keyboard = await _list_screen(
            tracking,
            user_id,
            display_timezone,
            await tracking.list_page_of(user_id, callback_data.product_id),
        )
        await edit_callback_message(
            callback,
            f"{text}\n\n{list_text}",
            reply_markup=keyboard,
            alert_text=text,
        )

    @router.callback_query(F.data.startswith(CB_LIST_PREFIXES))
    async def on_stale_list_button(callback: CallbackQuery) -> None:
        """Forged or malformed list payload: never touch the service."""
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)

    return router
