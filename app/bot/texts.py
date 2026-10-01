"""Single source of truth for user-facing Telegram texts.

All strings the user sees -- command replies, bot profile description and
short description, command menu entries -- live here instead of being
scattered across handlers (project convention, PROJECT.md §2.9). The price
disclaimer wordings themselves are not duplicated: they are imported from
``app.domain.money``, the single approved source (PROJECT.md §2.9).
"""

from typing import Final
from typing import assert_never

from aiogram.types import BotCommand

from app.domain.money import MAX_PRICE_KOPECKS
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.domain.money import format_price
from app.models.enums import AlertDirection
from app.schemas.marketplace import FetchFailureReason

START_TEXT: Final[str] = (
    "Привет! Я слежу за ценами товаров Wildberries и Ozon и сообщу, "
    "когда цена снизится или вырастет до заданного вами порога.\n\n"
    "Команды:\n"
    "/add — добавить товар в отслеживание\n"
    "/help — подробнее о боте\n"
    "/cancel — отменить текущее действие\n\n" + PRICE_DISCLAIMER_FULL
)

HELP_TEXT: Final[str] = (
    "Добавьте товар со ссылкой или артикулом Wildberries или Ozon и "
    "укажите пороговую цену: ниже текущей — сообщу о снижении, выше "
    "текущей — сообщу о росте. Я буду регулярно проверять цену и напишу "
    "вам, как только она дойдёт до заданного порога.\n\n"
    "Команды:\n"
    "/add — добавить товар в отслеживание\n"
    "/start — начать заново\n"
    "/cancel — отменить текущее действие\n\n" + PRICE_DISCLAIMER_FULL
)

CANCEL_TEXT: Final[str] = "Действие отменено."

NOTHING_TO_CANCEL_TEXT: Final[str] = (
    "Отменять нечего — сейчас нет незавершённого действия."
)

BOT_DESCRIPTION: Final[str] = (
    "Бот отслеживает цены товаров Wildberries и Ozon и уведомляет, когда "
    "цена снижается или растёт до заданного вами порога.\n\n" + PRICE_DISCLAIMER_FULL
)

BOT_SHORT_DESCRIPTION: Final[str] = (
    "Слежу за ценой Wildberries и Ozon и сообщаю о её снижении или росте. "
    f"{PRICE_DISCLAIMER_SHORT}."
)

BOT_COMMANDS: Final[list[BotCommand]] = [
    BotCommand(command="start", description="Начать и узнать, что умеет бот"),
    BotCommand(command="add", description="Добавить товар в отслеживание"),
    BotCommand(command="help", description="Подробнее о том, как пользоваться ботом"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]

# --- /add dialog ---------------------------------------------------------

ADD_PROMPT_LINK_TEXT: Final[str] = (
    "Пришлите ссылку на товар Wildberries или Ozon либо артикул Wildberries "
    "(только цифры). Чтобы выйти, нажмите «Отмена» или отправьте /cancel."
)

ADD_LINK_FORMAT_HINT_TEXT: Final[str] = (
    "Не получилось разобрать ссылку или артикул. Пришлите ссылку на страницу "
    "товара, например https://www.wildberries.ru/catalog/12345678/detail.aspx, "
    "или только артикул из цифр. Короткие ссылки не подойдут — откройте "
    "товар и скопируйте адрес из браузера."
)

ADD_NOT_TEXT_TEXT: Final[str] = (
    "Я понимаю только текст. Пришлите ссылку или артикул сообщением, "
    "либо нажмите «Отмена»."
)

ADD_MARKETPLACE_UNSUPPORTED_TEXT: Final[str] = (
    "Этот маркетплейс пока не поддерживается. Пришлите ссылку или артикул "
    "Wildberries либо нажмите «Отмена»."
)

ADD_FETCH_FAILURE_TEXTS: Final[dict[FetchFailureReason, str]] = {
    FetchFailureReason.not_found: (
        "Такой товар на маркетплейсе не нашёлся. Проверьте ссылку или артикул "
        "и пришлите ещё раз либо нажмите «Отмена»."
    ),
    FetchFailureReason.blocked: (
        "Маркетплейс заблокировал мой запрос: он защищается от автоматических "
        "обращений. Дело не в вашей ссылке. Можно прислать эту же ссылку ещё "
        "раз чуть позже или выбрать другой товар."
    ),
    FetchFailureReason.transport_error: (
        "Не удалось получить данные о товаре: маркетплейс не ответил или "
        "отклонил запрос. Отправьте ссылку ещё раз; если ошибка повторяется, "
        "проверьте ссылку или попробуйте другой товар."
    ),
    FetchFailureReason.bad_payload: (
        "Маркетплейс ответил в неожиданном виде, и я не смог прочитать данные "
        "о товаре. Это не ваша ошибка. Выберите другой товар или вернитесь к "
        "этому позже."
    ),
    FetchFailureReason.out_of_stock: (
        "Товара сейчас нет в наличии, поэтому цены у него нет. Пришлите другой "
        "товар или вернитесь к этому, когда он появится."
    ),
}

ADD_PROMPT_PRICE_TEXT: Final[str] = (
    "Укажите пороговую цену в рублях, например 1990 или 1990,50. Если она "
    "ниже текущей — сообщу, когда цена снизится до неё; если выше текущей — "
    "сообщу, когда цена вырастет до неё. Чтобы выйти, нажмите «Отмена»."
)

ADD_PRICE_INVALID_TEXT: Final[str] = (
    "Не получилось понять цену. Пришлите положительное число в рублях, "
    f"не более {format_price(MAX_PRICE_KOPECKS)}, с копейками не больше двух "
    "знаков, например 1990 или 1990,50."
)

ADD_TARGET_EQUALS_TEXT: Final[str] = (
    "Порог совпадает с текущей ценой. Укажите цену ниже или выше неё "
    "либо нажмите «Отмена»."
)

ADD_TARGET_EQUALS_RESTART_TEXT: Final[str] = (
    "Порог совпал с текущей ценой, поэтому отслеживание не добавлено. "
    "Начните заново командой /add и укажите цену ниже или выше текущей."
)

ADD_STALE_TEXT: Final[str] = "Это действие устарело. Начните заново командой /add."

ADD_STALE_ALERT_TEXT: Final[str] = "Кнопка устарела. Начните заново: /add"

ADD_OLD_CARD_ALERT_TEXT: Final[str] = "Эта карточка устарела — подтвердите последнюю."

ADD_CONFIRM_HINT_TEXT: Final[str] = (
    "Нажмите «Подтвердить», чтобы начать следить за ценой, или «Отмена»."
)

ADD_DUPLICATE_TEXT: Final[str] = (
    "Вы уже следите за этим товаром с таким порогом. Укажите другой порог "
    "через /add или откажитесь от добавления."
)

UNEXPECTED_ERROR_TEXT: Final[str] = (
    "Что-то пошло не так, и действие могло не выполниться. Попробуйте ещё "
    "раз; если ошибка повторится, повторите попытку чуть позже."
)

CONFIRM_BUTTON_TEXT: Final[str] = "Подтвердить"
CANCEL_BUTTON_TEXT: Final[str] = "Отмена"


def direction_phrase(direction: AlertDirection, price: int) -> str:
    """Wording of the trigger condition for a direction (single mapping)."""
    match direction:
        case AlertDirection.below:
            return f"цена станет ниже {format_price(price)}"
        case AlertDirection.above:
            return f"цена станет выше {format_price(price)}"
        case _:
            assert_never(direction)


def product_found_text(name: str, current_price: int) -> str:
    """Product card shown after a successful preview (plain text, no markup)."""
    return (
        f"Нашёл товар:\n{name}\n"
        f"Текущая цена: {format_price(current_price)}\n"
        f"{PRICE_DISCLAIMER_SHORT}.\n\n" + ADD_PROMPT_PRICE_TEXT
    )


def confirm_text(
    name: str, current_price: int, target_price: int, direction: AlertDirection
) -> str:
    """Confirmation step text (plain text, no markup)."""
    return (
        "Проверьте и подтвердите:\n"
        f"Товар: {name}\n"
        f"Текущая цена: {format_price(current_price)}\n"
        f"Сообщу, когда {direction_phrase(direction, target_price)}.\n"
        f"{PRICE_DISCLAIMER_SHORT}."
    )


def limit_exceeded_text(limit: int) -> str:
    """Product limit reached."""
    return (
        f"Достигнут лимит: можно следить не более чем за {limit} товарами. "
        "Чтобы добавить новый, сначала перестаньте следить за одним из текущих."
    )


def added_text(name: str, target_price: int, direction: AlertDirection) -> str:
    """Tracking added successfully (plain text, no markup)."""
    return (
        f"Отслеживание добавлено: {name}.\n"
        f"Сообщу, когда {direction_phrase(direction, target_price)}.\n"
        f"{PRICE_DISCLAIMER_SHORT}."
    )
