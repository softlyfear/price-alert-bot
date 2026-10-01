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
from typing import Any
from typing import assert_never

import httpx
from aiogram import BaseMiddleware
from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import CB_ADD_CANCEL
from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.keyboards import cancel_keyboard
from app.bot.keyboards import confirm_keyboard
from app.bot.states import AddProduct
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import InvalidPriceInputError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import ProductRefParseError
from app.domain.links import parse_product_ref
from app.domain.money import rubles_to_kopecks
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.client_factory import get_client
from app.services.tracking import TrackingService

_KEY_MARKETPLACE = "marketplace"
_KEY_ARTICLE = "article"
_KEY_NAME = "product_name"
_KEY_CURRENT_PRICE = "current_price"
_KEY_TARGET_PRICE = "target_price"

# Prevents a huge message from reaching the parser; the domain parser
# enforces its own, larger bound.
_MAX_TEXT_LENGTH = 4096


class TrackingServiceMiddleware(BaseMiddleware):
    """Build ``TrackingService`` for the update -- the single assembly point.

    Needs ``session`` (from ``DbSessionMiddleware``) plus ``http_client`` and
    ``max_products_per_user`` (dispatcher workflow data) in ``data``; hands
    the service to the handler as ``tracking``.
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
            client_factory=get_client,
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
    """Clear the dialog and report the outcome, answering the callback."""
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(text, parse_mode=None)


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

    @router.message(StateFilter(AddProduct.waiting_link), F.text)
    async def on_link(
        message: Message, state: FSMContext, tracking: TrackingService
    ) -> None:
        """Parse the link, show the product card or an honest refusal."""
        if message.from_user is None or message.text is None:
            return
        raw = message.text[:_MAX_TEXT_LENGTH]
        try:
            ref = parse_product_ref(raw)
        except ProductRefParseError:
            await message.answer(
                texts.ADD_LINK_FORMAT_HINT_TEXT, reply_markup=cancel_keyboard()
            )
            return
        try:
            result = await tracking.preview(
                message.from_user.id, ref.marketplace, ref.article
            )
        except ProductLimitExceededError as exc:
            await state.clear()
            await message.answer(texts.limit_exceeded_text(exc.limit))
            return
        except MarketplaceNotSupportedError:
            await message.answer(
                texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT, reply_markup=cancel_keyboard()
            )
            return
        if isinstance(result, MarketplaceFetchFailure):
            await message.answer(
                _failure_text(result.reason), reply_markup=cancel_keyboard()
            )
            return
        await state.update_data(
            {
                _KEY_MARKETPLACE: ref.marketplace.value,
                _KEY_ARTICLE: ref.article,
                _KEY_NAME: result.name,
                _KEY_CURRENT_PRICE: result.price,
            }
        )
        await state.set_state(AddProduct.waiting_target_price)
        await message.answer(
            texts.product_found_text(result.name, result.price),
            reply_markup=cancel_keyboard(),
            parse_mode=None,
        )

    @router.message(StateFilter(AddProduct.waiting_target_price), F.text)
    async def on_target_price(message: Message, state: FSMContext) -> None:
        """Convert rubles to kopecks and show the confirmation step."""
        if message.text is None:
            return
        try:
            target_price = rubles_to_kopecks(message.text[:_MAX_TEXT_LENGTH])
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
            await message.answer(texts.ADD_STALE_TEXT)
            return
        await state.update_data({_KEY_TARGET_PRICE: target_price})
        await state.set_state(AddProduct.confirming)
        await message.answer(
            texts.confirm_text(name, current_price, target_price),
            reply_markup=confirm_keyboard(),
            parse_mode=None,
        )

    @router.message(
        StateFilter(AddProduct.waiting_link, AddProduct.waiting_target_price),
        ~F.text,
    )
    async def on_not_text(message: Message) -> None:
        """Non-text input on a text step: explain, keep the state."""
        await message.answer(texts.ADD_NOT_TEXT_TEXT, reply_markup=cancel_keyboard())

    @router.message(StateFilter(AddProduct.confirming))
    async def on_text_while_confirming(message: Message) -> None:
        """Text instead of a button press: point at the buttons, keep the state."""
        await message.answer(
            texts.ADD_CONFIRM_HINT_TEXT, reply_markup=confirm_keyboard()
        )

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
            await tracking.add_tracking(
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
            await _finish(callback, state, texts.ADD_DUPLICATE_TEXT)
        except MarketplaceNotSupportedError:
            await _finish(callback, state, texts.ADD_MARKETPLACE_UNSUPPORTED_TEXT)
        else:
            await _finish(callback, state, texts.added_text(name, target_price))

    @router.callback_query(F.data == CB_ADD_CONFIRM)
    async def on_stale_confirm(callback: CallbackQuery) -> None:
        """Confirm pressed outside the confirmation step: never touch the service."""
        await callback.answer(texts.ADD_STALE_ALERT_TEXT, show_alert=True)

    return router
