"""Catch-all error handler: log the failure, answer the user honestly.

``aiogram`` wraps the whole update pipeline (including
``DbSessionMiddleware`` and its ``COMMIT``) in ``ErrorsMiddleware``, so a
failure anywhere reaches this handler. The handler never touches FSM state
and never re-raises: the original error is logged here with its traceback.
"""

from aiogram import Router
from aiogram.types import ErrorEvent
from aiogram.types import Message
from loguru import logger

from app.bot import texts


def create_router() -> Router:
    """Build a fresh ``errors`` router (see ``common.create_router``)."""
    router = Router(name="errors")

    @router.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """Log the failure with context and tell the user it may not have worked."""
        update = event.update
        log = logger.bind(update_id=update.update_id, update_type=update.event_type)
        log.opt(exception=event.exception).error("Unhandled error in update")
        callback = update.callback_query
        chat_message = update.message
        if callback is not None and isinstance(callback.message, Message):
            chat_message = callback.message
        if chat_message is not None:
            try:
                await chat_message.answer(texts.UNEXPECTED_ERROR_TEXT, parse_mode=None)
            except Exception as exc:
                log.warning("Failed to deliver the error reply: {exc}", exc=exc)
        if callback is not None:
            try:
                await callback.answer()
            except Exception as exc:
                log.warning("Failed to answer the callback: {exc}", exc=exc)
        return True

    return router
