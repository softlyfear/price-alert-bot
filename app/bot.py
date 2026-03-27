"""Telegram bot application entry point."""

from aiogram import Bot
from aiogram import Dispatcher

from app.core import get_settings

settings = get_settings()

bot = Bot(
    token=settings.tg.BOT_TOKEN,
)

dispatcher = Dispatcher()
