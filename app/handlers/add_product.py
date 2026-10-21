"""The /add dialog: link or article -> product card -> threshold -> confirm.

The handlers only parse input, call ``TrackingService`` and render replies.
Business rules (limit, duplicates) live in the service; domain exceptions are
mapped to Russian texts from ``app.bot.texts``. Callback payloads carry no
identifiers: the draft lives in FSM data, and every service call is scoped to
the Telegram user who sent the update.

Product names come from a marketplace (untrusted), so every message carrying
one is sent with ``parse_mode=None``.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from enum import Enum
from typing import Any
from typing import assert_never

import httpx
from aiogram import BaseMiddleware
from aiogram import F
from aiogram import Router
from aiogram.client.default import Default
from aiogram.exceptions import TelegramAPIError
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import InaccessibleMessage
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message
from aiogram.types import TelegramObject
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import CB_MENU_ADD
from app.bot.keyboards import AddRefCb
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import confirm_keyboard
from app.bot.keyboards import duplicate_exit_keyboard
from app.bot.keyboards import main_menu_keyboard
from app.bot.states import AddProduct
from app.bot.states import EditAlert
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import InvalidPriceInputError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import ProductRefParseError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.domain.links import ProductRef
from app.domain.links import parse_product_ref
from app.domain.money import rubles_to_kopecks
from app.domain.thresholds import choose_direction
from app.handlers.callback_reply import edit_callback_message
from app.handlers.callback_reply import reply_to_callback
from app.handlers.product_card import card_screen
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.tracking import ProductCard
from app.services.tracking import TrackingService

_KEY_MARKETPLACE = "marketplace"
_KEY_ARTICLE = "article"
_KEY_NAME = "product_name"
_KEY_CURRENT_PRICE = "current_price"
_KEY_TARGET_PRICE = "target_price"
_KEY_CONFIRM_MESSAGE_ID = "confirm_message_id"
_KEY_LINK_MESSAGE_ID = "link_message_id"

# aiogram's marker for "use the bot-wide parse_mode"; same as ``answer`` default.
_BOT_DEFAULT_PARSE_MODE = Default("parse_mode")

# Prevents a huge message from reaching the parser; the domain parser
# enforces its own, larger bound.
MAX_TEXT_LENGTH = 4096


class TrackingServiceMiddleware(BaseMiddleware):
    """Build ``TrackingService`` for the update -- the single assembly point.

    Needs ``session`` (from ``DbSessionMiddleware``) plus ``http_client``,
    ``max_products_per_user`` and ``client_factory`` (dispatcher workflow data)
    in ``data``; hands the service to the handler as ``tracking``.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Put a ``TrackingService`` into ``data`` and continue."""
        session: AsyncSession = data["session"]
        http_client: httpx.AsyncClient = data["http_client"]
        data["tracking"] = TrackingService(
            session=session,
            http_client=http_client,
            client_factory=data["client_factory"],
            max_products_per_user=data["max_products_per_user"],
        )
        return await handler(event, data)


def _failure_text(reason: FetchFailureReason) -> str:
    match reason:
        case (
            FetchFailureReason.not_found
            | FetchFailureReason.blocked
            | FetchFailureReason.transport_error
            | FetchFailureReason.bad_payload
            | FetchFailureReason.out_of_stock
        ):
            return texts.ADD_FETCH_FAILURE_TEXTS[reason]
        case _:
            assert_never(reason)


async def _finish(callback: CallbackQuery, state: FSMContext, text: str) -> None:
    """Clear the dialog, then report the outcome through the single exit point."""
    await state.clear()
    await reply_to_callback(callback, text, reply_markup=main_menu_keyboard())


async def _send_checking_notice(message: Message) -> Message | None:
    """Send the "checking" notice; a Bot API refusal must not break the step."""
    try:
        return await message.answer(texts.ADD_CHECKING_TEXT)
    except TelegramAPIError as exc:
        logger.bind(error_type=type(exc).__name__).warning(
            "Could not send the checking notice"
        )
        return None


async def _discard_if_stale(
    marker: int, notice: Message | None, state: FSMContext, user_id: int
) -> bool:
    """Report whether this link step was superseded; if so, drop its outcome.

    The step is current while the dialog still waits for a link and the
    FSM marker equals ``marker``. A stale step only retitles its notice.
    """
    reason = "state_changed"
    if await state.get_state() == AddProduct.waiting_link.state:
        data = await state.get_data()
        if data.get(_KEY_LINK_MESSAGE_ID) == marker:
            return False
        reason = "marker_mismatch"
    logger.bind(user_id=user_id, reason=reason).info(
        "Product check result dropped: the dialog moved on"
    )
    if notice is not None:
        try:
            await notice.edit_text(texts.ADD_CHECK_DROPPED_TEXT, parse_mode=None)
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).warning(
                "Could not retitle the dropped checking notice"
            )
    return True


class _Skip(Enum):
    """Why the tracked-product lookup produced no card."""

    no_match = "no_match"
    stale = "stale"


async def _lookup_tracked(
    marker: int,
    ref: ProductRef,
    user_id: int,
    state: FSMContext,
    tracking: TrackingService,
) -> ProductCard | _Skip:
    """Find the caller's already tracked product for this link step.

    A card is returned only for a current step, with the dialog already
    cleared; a superseded step yields ``stale`` and changes nothing.
    """
    card = await tracking.find_tracked_product(user_id, ref.marketplace, ref.article)
    if card is None:
        return _Skip.no_match
    if await _discard_if_stale(marker, None, state, user_id):
        return _Skip.stale
    await state.clear()
    return card


def _already_tracked_screen(card: ProductCard) -> tuple[str, InlineKeyboardMarkup]:
    """Product card prefixed with the "already tracked" line."""
    text, keyboard = card_screen(card)
    return f"{texts.ADD_ALREADY_TRACKED_TEXT}\n\n{text}", keyboard


async def enter_price_step(
    state: FSMContext, marketplace: Marketplace, article: int, name: str, price: int
) -> None:
    """Move the dialog to the threshold step with the product draft in FSM."""
    await state.update_data(
        {
            _KEY_MARKETPLACE: marketplace.value,
            _KEY_ARTICLE: article,
            _KEY_NAME: name,
            _KEY_CURRENT_PRICE: price,
        }
    )
    await state.set_state(AddProduct.waiting_target_price)


async def _deliver_outcome(
    message: Message | InaccessibleMessage,
    notice: Message | None,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | Default | None = _BOT_DEFAULT_PARSE_MODE,
) -> None:
    """Show the step outcome by editing the notice, else as a new message.

    Only ``TelegramBadRequest`` (message gone or not editable) falls back to
    a new message; other errors propagate to the ``errors`` handler.
    """
    if notice is not None:
        try:
            await notice.edit_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
        except TelegramBadRequest:
            pass
        else:
            return
    await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def _check_and_deliver(
    anchor: Message | InaccessibleMessage,
    marker: int,
    notice: Message | None,
    ref: ProductRef,
    user_id: int,
    state: FSMContext,
    tracking: TrackingService,
) -> None:
    """Check the product and deliver the outcome of the link step.

    Shared by the link message and the "add this product" button. ``marker``
    is the id already stored in FSM; ``anchor`` takes a new message when the
    notice cannot be edited.
    """
    try:
        result = await tracking.preview(user_id, ref.marketplace, ref.article)
    except ProductLimitExceededError as exc:
        if await _discard_if_stale(marker, notice, state, user_id):
            return
        await state.clear()
        await _deliver_outcome(
            anchor,
            notice,
            texts.limit_exceeded_text(exc.limit),
            reply_markup=main_menu_keyboard(),
        )
        return
    except MarketplaceNotSupportedError:
        if await _discard_if_stale(marker, notice, state, user_id):
            return
        await _deliver_outcome(
            anchor,
            notice,
            texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT,
            reply_markup=cancel_keyboard(),
        )
        return
    if await _discard_if_stale(marker, notice, state, user_id):
        return
    if isinstance(result, MarketplaceFetchFailure):
        await _deliver_outcome(
            anchor,
            notice,
            _failure_text(result.reason),
            reply_markup=cancel_keyboard(),
        )
        return
    # Accepted window: an interrupt landing between the check above and
    # these writes (a few Redis round-trips) can still be overwritten.
    await enter_price_step(
        state, ref.marketplace, ref.article, result.name, result.price
    )
    await _deliver_outcome(
        anchor,
        notice,
        texts.product_found_text(result.name, result.price),
        reply_markup=cancel_keyboard(),
        parse_mode=None,
    )


def create_router() -> Router:
    """Build a fresh ``add_product`` router (see ``common.create_router``)."""
    router = Router(name="add_product")
    middleware = TrackingServiceMiddleware()
    router.message.middleware(middleware)
    router.callback_query.middleware(middleware)

    @router.message(Command("add"))
    async def cmd_add(message: Message, state: FSMContext) -> None:
        """Start the dialog (restarts it if one is already running)."""
        await state.clear()
        await state.set_state(AddProduct.waiting_link)
        await message.answer(texts.ADD_PROMPT_LINK_TEXT, reply_markup=cancel_keyboard())

    @router.callback_query(F.data == CB_MENU_ADD)
    async def on_menu_add(callback: CallbackQuery, state: FSMContext) -> None:
        """ "Add a product" button: same effect as /add, edited in place."""
        await state.clear()
        await state.set_state(AddProduct.waiting_link)
        await edit_callback_message(
            callback,
            texts.ADD_PROMPT_LINK_TEXT,
            reply_markup=cancel_keyboard(),
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.callback_query(AddRefCb.filter())
    async def on_add_ref(
        callback: CallbackQuery,
        callback_data: AddRefCb,
        state: FSMContext,
        tracking: TrackingService,
    ) -> None:
        """ "Add this product" button: restart the dialog at the link step."""
        message = callback.message
        if message is None:
            await callback.answer(texts.ADD_STALE_ALERT_TEXT, show_alert=True)
            return
        await state.clear()
        await state.set_state(AddProduct.waiting_link)
        await state.update_data({_KEY_LINK_MESSAGE_ID: message.message_id})
        ref = ProductRef(
            marketplace=callback_data.marketplace, article=callback_data.article
        )
        found = await _lookup_tracked(
            message.message_id, ref, callback.from_user.id, state, tracking
        )
        if isinstance(found, ProductCard):
            text, keyboard = _already_tracked_screen(found)
            await edit_callback_message(
                callback,
                text,
                reply_markup=keyboard,
                alert_text=texts.ADD_STALE_ALERT_TEXT,
            )
            return
        if found is _Skip.stale:
            await callback.answer(texts.ADD_STALE_ALERT_TEXT, show_alert=True)
            return
        notice = await edit_callback_message(callback, texts.ADD_CHECKING_TEXT)
        await _check_and_deliver(
            message,
            message.message_id,
            notice,
            ref,
            callback.from_user.id,
            state,
            tracking,
        )

    @router.callback_query(F.data.startswith(AddRefCb.__prefix__))
    async def on_stale_add_ref(callback: CallbackQuery) -> None:
        """Forged or malformed payload: never touch the service."""
        await callback.answer(texts.ADD_STALE_ALERT_TEXT, show_alert=True)

    @router.message(StateFilter(AddProduct.waiting_link), F.text)
    async def on_link(
        message: Message, state: FSMContext, tracking: TrackingService
    ) -> None:
        """Parse the link, show the product card or an honest refusal."""
        if message.from_user is None or message.text is None:
            return
        raw = message.text[:MAX_TEXT_LENGTH]
        try:
            ref = parse_product_ref(raw)
        except ProductRefParseError:
            await message.answer(
                texts.ADD_LINK_FORMAT_HINT_TEXT, reply_markup=cancel_keyboard()
            )
            return
        # Window accepted: RedisStorage.update_data reads and writes in separate steps.
        await state.update_data({_KEY_LINK_MESSAGE_ID: message.message_id})
        found = await _lookup_tracked(
            message.message_id, ref, message.from_user.id, state, tracking
        )
        if isinstance(found, ProductCard):
            text, keyboard = _already_tracked_screen(found)
            await message.answer(text, reply_markup=keyboard, parse_mode=None)
            return
        if found is _Skip.stale:
            return
        notice = await _send_checking_notice(message)
        await _check_and_deliver(
            message,
            message.message_id,
            notice,
            ref,
            message.from_user.id,
            state,
            tracking,
        )

    @router.message(StateFilter(AddProduct.waiting_target_price), F.text)
    async def on_target_price(message: Message, state: FSMContext) -> None:
        """Convert rubles to kopecks and show the confirmation step."""
        if message.text is None:
            return
        try:
            target_price = rubles_to_kopecks(message.text[:MAX_TEXT_LENGTH])
        except InvalidPriceInputError:
            await message.answer(
                texts.ADD_PRICE_INVALID_TEXT, reply_markup=cancel_keyboard()
            )
            return
        data = await state.get_data()
        name = data.get(_KEY_NAME)
        current_price = data.get(_KEY_CURRENT_PRICE)
        if not isinstance(name, str) or not isinstance(current_price, int):
            await state.clear()
            await message.answer(
                texts.ADD_STALE_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        try:
            direction = choose_direction(current_price, target_price)
        except TargetEqualsCurrentPriceError:
            await message.answer(
                texts.ADD_TARGET_EQUALS_TEXT, reply_markup=cancel_keyboard()
            )
            return
        await state.update_data({_KEY_TARGET_PRICE: target_price})
        await state.set_state(AddProduct.confirming)
        card = await message.answer(
            texts.confirm_text(name, current_price, target_price, direction),
            reply_markup=confirm_keyboard(),
            parse_mode=None,
        )
        await state.update_data({_KEY_CONFIRM_MESSAGE_ID: card.message_id})

    @router.message(
        StateFilter(AddProduct.waiting_link, AddProduct.waiting_target_price),
        ~F.text,
    )
    async def on_not_text(message: Message) -> None:
        """Non-text input on a text step: explain, keep the state."""
        await message.answer(texts.ADD_NOT_TEXT_TEXT, reply_markup=cancel_keyboard())

    @router.message(StateFilter(AddProduct.confirming))
    async def on_text_while_confirming(message: Message, state: FSMContext) -> None:
        """Text instead of a button press: point at the buttons, keep the state.

        The hint carries a fresh "Confirm" button, so it becomes the only
        live card; the previous one turns stale.
        """
        hint = await message.answer(
            texts.ADD_CONFIRM_HINT_TEXT, reply_markup=confirm_keyboard()
        )
        await state.update_data({_KEY_CONFIRM_MESSAGE_ID: hint.message_id})

    @router.callback_query(StateFilter(EditAlert), F.data == CB_ADD_CANCEL)
    async def on_stale_cancel(callback: CallbackQuery) -> None:
        """Old "Cancel" of the /add dialog must not end a threshold edit."""
        await callback.answer(texts.LIST_STALE_BUTTON_ALERT_TEXT, show_alert=True)

    @router.callback_query(F.data == CB_ADD_CANCEL)
    async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        """Cancel from any step; clearing an empty state is harmless."""
        await _finish(callback, state, texts.CANCEL_TEXT)

    @router.callback_query(StateFilter(AddProduct.confirming), F.data == CB_ADD_CONFIRM)
    async def on_confirm(
        callback: CallbackQuery, state: FSMContext, tracking: TrackingService
    ) -> None:
        """Create the product and the alert for the pressing user only."""
        data = await state.get_data()
        if (
            not isinstance(callback.message, Message)
            or data.get(_KEY_CONFIRM_MESSAGE_ID) != callback.message.message_id
        ):
            await callback.answer(texts.ADD_OLD_CARD_ALERT_TEXT, show_alert=True)
            return
        try:
            marketplace = Marketplace(data[_KEY_MARKETPLACE])
            article = data[_KEY_ARTICLE]
            name = data[_KEY_NAME]
            current_price = data[_KEY_CURRENT_PRICE]
            target_price = data[_KEY_TARGET_PRICE]
        except KeyError, ValueError:
            await _finish(callback, state, texts.ADD_STALE_TEXT)
            return
        if (
            not isinstance(article, int)
            or not isinstance(name, str)
            or not isinstance(current_price, int)
            or not isinstance(target_price, int)
        ):
            await _finish(callback, state, texts.ADD_STALE_TEXT)
            return
        try:
            alert = await tracking.add_tracking(
                callback.from_user.id,
                marketplace,
                article,
                name,
                current_price,
                target_price,
            )
        except ProductLimitExceededError as exc:
            await _finish(callback, state, texts.limit_exceeded_text(exc.limit))
        except DuplicateAlertError:
            await state.set_state(AddProduct.waiting_target_price)
            await edit_callback_message(
                callback,
                texts.ALERT_DUPLICATE_TEXT,
                reply_markup=duplicate_exit_keyboard(),
                alert_text=texts.ALERT_DUPLICATE_TEXT,
            )
        except MarketplaceNotSupportedError:
            await _finish(callback, state, texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT)
        except TargetEqualsCurrentPriceError:
            await _finish(callback, state, texts.ADD_TARGET_EQUALS_RESTART_TEXT)
        else:
            await _finish(
                callback,
                state,
                texts.added_text(name, target_price, alert.direction),
            )

    @router.callback_query(F.data == CB_ADD_CONFIRM)
    async def on_stale_confirm(callback: CallbackQuery) -> None:
        """Confirm pressed outside the confirmation step: never touch the service."""
        await callback.answer(texts.ADD_STALE_ALERT_TEXT, show_alert=True)

    return router
