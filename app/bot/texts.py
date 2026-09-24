"""Single source of truth for user-facing Telegram texts.

All strings the user sees -- command replies, bot profile description and
short description, command menu entries -- live here instead of being
scattered across handlers (project convention, PROJECT.md §2.9). The price
disclaimer wordings themselves are not duplicated: they are imported from
``app.domain.money``, the single approved source (PROJECT.md §2.9).
"""

from typing import Final

from aiogram.types import BotCommand

from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT

START_TEXT: Final[str] = (
    "Привет! Я слежу за ценами товаров Wildberries и Ozon и сообщу, "
    "когда цена станет ниже заданного вами порога.\n\n"
    "Команды:\n"
    "/help — подробнее о боте\n"
    "/cancel — отменить текущее действие\n\n" + PRICE_DISCLAIMER_FULL
)

HELP_TEXT: Final[str] = (
    "Добавьте товар со ссылкой или артикулом Wildberries или Ozon и "
    "укажите цену, ниже которой хотите получить уведомление. Я буду "
    "регулярно проверять цену и напишу вам, как только она опустится "
    "до заданного порога.\n\n"
    "Команды:\n"
    "/start — начать заново\n"
    "/cancel — отменить текущее действие\n\n" + PRICE_DISCLAIMER_FULL
)

CANCEL_TEXT: Final[str] = "Действие отменено."

NOTHING_TO_CANCEL_TEXT: Final[str] = (
    "Отменять нечего — сейчас нет незавершённого действия."
)

BOT_DESCRIPTION: Final[str] = (
    "Бот отслеживает цены товаров Wildberries и Ozon и уведомляет, когда "
    "цена опускается ниже заданного вами порога.\n\n" + PRICE_DISCLAIMER_FULL
)

BOT_SHORT_DESCRIPTION: Final[str] = (
    f"Слежу за ценой Wildberries и Ozon и сообщаю о снижении. {PRICE_DISCLAIMER_SHORT}."
)

BOT_COMMANDS: Final[list[BotCommand]] = [
    BotCommand(command="start", description="Начать и узнать, что умеет бот"),
    BotCommand(command="help", description="Подробнее о том, как пользоваться ботом"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]
