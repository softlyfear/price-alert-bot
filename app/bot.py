"""Telegram bot application entry point."""

from aiogram import Bot
from aiogram import Dispatcher

from app.core import settings

bot = Bot(
    token=settings.tg.BOT_TOKEN,
)

dispatcher = Dispatcher()
