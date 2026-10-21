"""Handlers for the /start, /help and /cancel commands.

Included first via ``app.handlers.create_root_router`` so these commands
take priority over any dialog handlers registered by later tickets (Р8).
"""

from aiogram import F
from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import CB_MENU_ALL
from app.bot.keyboards import CB_MENU_HELP
from app.bot.keyboards import CB_MENU_PREFIX
from app.bot.keyboards import CB_MENU_START
from app.bot.keyboards import CB_NOTICE_MENU
from app.bot.keyboards import main_menu_keyboard
from app.handlers.callback_reply import edit_callback_message
from app.handlers.callback_reply import reply_to_callback


def create_router() -> Router:
    """Build a fresh ``common`` router with /start, /help, /cancel handlers.

    A factory (instead of a module-level singleton) lets callers build the
    router repeatedly in one process -- ``aiogram`` raises ``RuntimeError``
    if the same ``Router`` instance is included into a dispatcher twice.
    """
    router = Router(name="common")

    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        """Greet the user. Does not touch FSM state (Р8)."""
        await message.answer(texts.START_TEXT, reply_markup=main_menu_keyboard())

    @router.message(Command("help"))
    async def cmd_help(message: Message, max_products_per_user: int) -> None:
        """Explain how the bot works. Does not touch FSM state (Р8)."""
        await message.answer(
            texts.help_text(max_products_per_user), reply_markup=main_menu_keyboard()
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        """Reset FSM state from any state; report separately if nothing to cancel."""
        current_state = await state.get_state()
        if current_state is None:
            await message.answer(
                texts.NOTHING_TO_CANCEL_TEXT, reply_markup=main_menu_keyboard()
            )
            return
        await state.clear()
        await message.answer(texts.CANCEL_TEXT, reply_markup=main_menu_keyboard())

    @router.callback_query(F.data == CB_MENU_START)
    async def on_menu_start(callback: CallbackQuery) -> None:
        """ "Main menu" button: the /start screen in place; FSM untouched."""
        await edit_callback_message(
            callback,
            texts.START_TEXT,
            reply_markup=main_menu_keyboard(),
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.callback_query(F.data == CB_NOTICE_MENU)
    async def on_notice_menu(callback: CallbackQuery) -> None:
        """Notification "Main menu": a new message, the notice stays; FSM untouched."""
        await reply_to_callback(
            callback,
            texts.START_TEXT,
            reply_markup=main_menu_keyboard(),
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.callback_query(F.data == CB_MENU_HELP)
    async def on_menu_help(callback: CallbackQuery, max_products_per_user: int) -> None:
        """ "Help" button: the /help screen in place; FSM untouched."""
        await edit_callback_message(
            callback,
            texts.help_text(max_products_per_user),
            reply_markup=main_menu_keyboard(),
            alert_text=texts.MENU_STALE_ALERT_TEXT,
        )

    @router.callback_query(
        F.data.startswith(CB_MENU_PREFIX), F.data.not_in(CB_MENU_ALL)
    )
    async def on_unknown_menu(callback: CallbackQuery) -> None:
        """Unknown menu payload: popup only, never touch the service or FSM."""
        await callback.answer(texts.MENU_STALE_ALERT_TEXT, show_alert=True)

    return router
