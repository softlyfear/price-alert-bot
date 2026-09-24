"""Handlers for the /start, /help and /cancel commands.

Included first via ``app.handlers.create_root_router`` so these commands
take priority over any dialog handlers registered by later tickets (Р8).
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import texts


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
        await message.answer(texts.START_TEXT)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        """Explain how the bot works. Does not touch FSM state (Р8)."""
        await message.answer(texts.HELP_TEXT)

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        """Reset FSM state from any state; report separately if nothing to cancel."""
        current_state = await state.get_state()
        if current_state is None:
            await message.answer(texts.NOTHING_TO_CANCEL_TEXT)
            return
        await state.clear()
        await message.answer(texts.CANCEL_TEXT)

    return router
