"""Telegram bot application entry point."""

import asyncio

from aiogram import Bot, Dispatcher

from app.core import settings

bot = Bot(
    token=settings.tg.bot_token,
)

dispatcher = Dispatcher()
