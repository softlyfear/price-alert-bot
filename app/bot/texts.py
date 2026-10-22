"""Single source of truth for user-facing Telegram texts.

All strings the user sees -- command replies, bot profile description and
short description, command menu entries -- live here instead of being
scattered across handlers (project convention, PROJECT.md §2.9). The price
disclaimer wordings themselves are not duplicated: they are imported from
``app.domain.money``, the single approved source (PROJECT.md §2.9).
"""

from datetime import datetime
from typing import Final
from typing import assert_never
from zoneinfo import ZoneInfo

from aiogram.types import BotCommand

from app.domain.money import MAX_PRICE_KOPECKS
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.domain.money import format_price
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshReport

SOUND_REMINDER_TEXT: Final[str] = (
    "Чтобы не пропустить сигнал о цене, включите уведомления от этого чата."
)

START_TEXT: Final[str] = (
    "Привет! Я слежу за ценами товаров Wildberries и Ozon и сообщу, "
    "когда цена снизится или вырастет до заданного вами порога.\n\n"
    + SOUND_REMINDER_TEXT
    + "\n\n"
    + PRICE_DISCLAIMER_FULL
)

MENU_ADD_BUTTON_TEXT: Final[str] = "➕ Добавить товар"
MENU_LIST_BUTTON_TEXT: Final[str] = "Мои товары"
MENU_HELP_BUTTON_TEXT: Final[str] = "Помощь"
MENU_MAIN_BUTTON_TEXT: Final[str] = "Главное меню"
NOTICE_OPEN_BUTTON_TEXT: Final[str] = "Открыть товар"

FALLBACK_TEXT: Final[str] = (
    "Не понял сообщение. Выберите действие ниже или пришлите ссылку на "
    "товар Wildberries или Ozon, либо артикул из цифр."
)

ADD_REF_BUTTON_TEXT: Final[str] = "➕ Добавить этот товар"

MENU_STALE_ALERT_TEXT: Final[str] = (
    "Кнопка устарела. Воспользуйтесь кнопками последнего сообщения бота."
)


def help_text(max_products: int) -> str:
    """Detailed help; the caller's effective product limit."""
    return (
        "Я слежу за ценами на Wildberries и Ozon и пишу вам, когда цена "
        "товара доходит до нужного вам порога.\n\n"
        "Как добавить товар. Нажмите «➕ Добавить товар» и пришлите ссылку на "
        "товар Wildberries или Ozon либо только артикул из цифр: его я ищу "
        "сначала на Wildberries, затем на Ozon. Я покажу найденный товар и его "
        "текущую цену, затем попрошу указать порог в рублях.\n\n"
        "Как работает порог. Если порог ниже текущей цены, я сообщу, когда "
        "цена снизится до него. Если выше — когда цена вырастет до него. "
        "Порог, равный текущей цене, не подойдёт. Сработав, порог остаётся "
        "активным: я напишу снова, если цена опять окажется за порогом, но "
        "не чаще, чем раз в установленный интервал.\n\n"
        f"Лимит товаров: {max_products}.\n\n"
        "Свои товары и пороги видны в «Мои товары». Там же, в карточке товара, можно "
        "нажать «Добавить порог», изменить или убрать существующий порог либо "
        "перестать следить за товаром.\n\n"
        "Ozon защищается от автоматических запросов и иногда блокирует их. "
        "Тогда я скажу об этом честно; обычно помогает повторить попытку "
        "чуть позже.\n\n" + SOUND_REMINDER_TEXT + "\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/add — добавить товар в отслеживание\n"
        "/list — мои товары и пороги\n"
        "/help — эта справка\n"
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
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Как пользоваться ботом"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]

# --- /add dialog ---------------------------------------------------------

ADD_PROMPT_LINK_TEXT: Final[str] = (
    "Пришлите ссылку на товар Wildberries или Ozon либо артикул из цифр: "
    "я поищу его сначала на Wildberries, затем на Ozon. Чтобы выйти, "
    "нажмите «Отмена»."
)

ADD_CHECKING_TEXT: Final[str] = (
    "Проверяю товар — это может занять некоторое время, пожалуйста, подождите."
)

ADD_CHECK_DROPPED_TEXT: Final[str] = "Проверка прервана."

ADD_LINK_FORMAT_HINT_TEXT: Final[str] = (
    "Не получилось разобрать ссылку или артикул. Пришлите ссылку на страницу "
    "товара, например https://www.wildberries.ru/catalog/12345678/detail.aspx, "
    "или только артикул из цифр. Короткая ссылка Ozon «Поделиться» "
    "тоже подойдёт; сокращённые ссылки Wildberries я пока не раскрываю — "
    "откройте товар и скопируйте адрес из браузера."
)

MULTIPLE_PRODUCT_REFS_TEXT: Final[str] = (
    "В сообщении ссылки на разные товары. Пришлите ссылку на один товар."
)

ADD_NOT_TEXT_TEXT: Final[str] = (
    "Я понимаю только текст. Пришлите ссылку или артикул сообщением, "
    "либо нажмите «Отмена»."
)

ADD_MARKETPLACE_UNSUPPORTED_TEXT: Final[str] = (
    "Этот маркетплейс пока не поддерживается."
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

ARTICLE_WB_FAILURE_TEXTS: Final[dict[FetchFailureReason, str]] = {
    FetchFailureReason.not_found: (
        "Такой товар на Wildberries не нашёлся. Проверьте номер и пришлите "
        "ещё раз либо нажмите «Отмена»."
    ),
    FetchFailureReason.blocked: (
        "Wildberries заблокировал мой запрос: он защищается от автоматических "
        "обращений. Дело не в вашем номере. Можно прислать его ещё раз чуть "
        "позже или выбрать другой товар."
    ),
    FetchFailureReason.transport_error: (
        "Не удалось получить данные с Wildberries: он не ответил или "
        "отклонил запрос. Отправьте номер ещё раз; если ошибка повторяется, "
        "попробуйте позже или другой товар."
    ),
    FetchFailureReason.bad_payload: (
        "Wildberries ответил в неожиданном виде, и я не смог прочитать данные "
        "о товаре. Это не ваша ошибка. Выберите другой товар или вернитесь к "
        "этому позже."
    ),
    FetchFailureReason.out_of_stock: (
        "Товара сейчас нет в наличии на Wildberries, поэтому цены у него "
        "нет. Пришлите другой товар или вернитесь к этому, когда он появится."
    ),
}

ARTICLE_OZON_FAILURE_TEXTS: Final[dict[FetchFailureReason, str]] = {
    FetchFailureReason.not_found: (
        "Такой товар не нашёлся ни на Wildberries, ни на Ozon. Проверьте "
        "номер и пришлите ещё раз либо нажмите «Отмена»."
    ),
    FetchFailureReason.bad_payload: (
        "На Wildberries такого товара нет, а Ozon не вернул данные о товаре "
        "с этим номером. Проверьте номер или пришлите ссылку на товар."
    ),
    FetchFailureReason.blocked: (
        "На Wildberries такого товара нет, а проверить Ozon сейчас не "
        "удалось: он защищается от автоматических обращений. Попробуйте "
        "прислать номер чуть позже или пришлите ссылку на товар."
    ),
    FetchFailureReason.transport_error: (
        "На Wildberries такого товара нет, а проверить Ozon сейчас не "
        "удалось: он не ответил. Попробуйте прислать номер чуть позже или "
        "пришлите ссылку на товар."
    ),
    FetchFailureReason.out_of_stock: (
        "На Wildberries такого товара нет, а на Ozon он есть, но сейчас не "
        "в наличии, поэтому цены у него нет. Пришлите другой товар или "
        "вернитесь к этому, когда он появится."
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
    "Начните заново кнопкой «➕ Добавить товар» и укажите цену ниже или выше "
    "текущей."
)

ADD_STALE_TEXT: Final[str] = (
    "Это действие устарело. Начните заново кнопкой «➕ Добавить товар»."
)

ADD_STALE_ALERT_TEXT: Final[str] = (
    "Кнопка устарела. Чтобы добавить товар, нажмите «➕ Добавить товар» в главном меню."
)

ADD_OLD_CARD_ALERT_TEXT: Final[str] = "Эта карточка устарела — подтвердите последнюю."

ADD_CONFIRM_HINT_TEXT: Final[str] = (
    "Нажмите «Подтвердить», чтобы начать следить за ценой, или «Отмена»."
)

ALERT_DUPLICATE_TEXT: Final[str] = (
    "У вас уже есть такой порог. Введите другую сумму в рублях, например "
    "1990 или 1990,50."
)

LIST_BACK_TO_LIST_BUTTON_TEXT: Final[str] = "← К списку товаров"

UNEXPECTED_ERROR_TEXT: Final[str] = (
    "Что-то пошло не так, и действие могло не выполниться. Попробуйте ещё "
    "раз; если ошибка повторится, повторите попытку чуть позже."
)

# --- /list: products, card, removal ----------------------------------------

LIST_EMPTY_TEXT: Final[str] = (
    "Вы пока ни за чем не следите. Нажмите «➕ Добавить товар» ниже, "
    "чтобы добавить первый."
)

LIST_STALE_BUTTON_ALERT_TEXT: Final[str] = (
    "Кнопка устарела. Откройте актуальный список: «Главное меню» → «Мои товары»."
)

LIST_GONE_ALERT_TEXT: Final[str] = (
    "Товар или порог уже не отслеживается. Откройте актуальный список: "
    "«Главное меню» → «Мои товары»."
)

ALERT_REMOVED_TEXT: Final[str] = "Порог удалён"

ALERT_ALREADY_REMOVED_TEXT: Final[str] = "Порог уже удалён"

PRODUCT_ALREADY_GONE_TEXT: Final[str] = "Товар уже не отслеживается"

ADD_ALREADY_TRACKED_TEXT: Final[str] = "Товар уже отслеживается"

LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT: Final[str] = (
    "Порог удалён. Других порогов у товара не было, поэтому я перестал за ним "
    "следить. Чтобы вернуть товар, добавьте его заново: "
    "«Главное меню» → «➕ Добавить товар»."
)

LIST_PRODUCT_REMOVED_TEXT: Final[str] = (
    "Больше не слежу за этим товаром, его пороги удалены. Чтобы вернуть "
    "товар, добавьте его заново: «Главное меню» → «➕ Добавить товар»."
)

ADD_ALERT_BUTTON_TEXT: Final[str] = "➕ Добавить порог"

STOP_TRACKING_BUTTON_TEXT: Final[str] = "Перестать следить"
LIST_BACK_BUTTON_TEXT: Final[str] = "← К списку"
LIST_PREV_BUTTON_TEXT: Final[str] = "◀"
LIST_NEXT_BUTTON_TEXT: Final[str] = "▶"

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


def direction_arrow(direction: AlertDirection) -> str:
    """Short marker of a direction used on cards and buttons (single mapping)."""
    match direction:
        case AlertDirection.below:
            return "📉"
        case AlertDirection.above:
            return "📈"
        case _:
            assert_never(direction)


def add_ref_offer_text(marketplace: Marketplace, article: int) -> str:
    """Offer to add a product recognised in free text (plain text, no markup)."""
    return f"Добавить этот товар? {marketplace_title(marketplace)}, артикул {article}."


def add_article_offer_text(article: int) -> str:
    """Offer for a bare article in free text: no single marketplace is named."""
    return (
        f"Добавить этот товар? Артикул {article}: поищу его сначала на "
        "Wildberries, затем на Ozon."
    )


def add_short_link_offer_text() -> str:
    """Offer for an Ozon short link in free text (the code is not echoed)."""
    return "Добавить этот товар? Ozon, короткая ссылка."


SHORT_LINK_UNRESOLVED_TEXT: Final[str] = (
    "Не получилось раскрыть короткую ссылку Ozon. Пришлите полную ссылку со "
    "страницы товара: откройте товар в браузере и скопируйте адрес. Либо "
    "нажмите «Отмена»."
)

_SHORT_LINK_NOT_FOUND_TEXT: Final[str] = (
    "Короткая ссылка Ozon не найдена или устарела. Проверьте её и пришлите "
    "ещё раз либо пришлите полную ссылку на товар."
)

_SHORT_LINK_BLOCKED_TEXT: Final[str] = (
    "Ozon заблокировал мой запрос: он защищается от автоматических "
    "обращений. Дело не в вашей ссылке. Попробуйте прислать её чуть позже "
    "или пришлите полную ссылку на товар."
)

_SHORT_LINK_TRANSPORT_TEXT: Final[str] = (
    "Не удалось раскрыть короткую ссылку: Ozon не ответил или отклонил "
    "запрос. Попробуйте прислать её чуть позже или пришлите полную ссылку "
    "на товар."
)


def short_link_failure_text(reason: FetchFailureReason) -> str:
    """Refusal for a short link whose expansion failed, naming Ozon."""
    match reason:
        case FetchFailureReason.bad_payload | FetchFailureReason.out_of_stock:
            return SHORT_LINK_UNRESOLVED_TEXT
        case FetchFailureReason.not_found:
            return _SHORT_LINK_NOT_FOUND_TEXT
        case FetchFailureReason.blocked:
            return _SHORT_LINK_BLOCKED_TEXT
        case FetchFailureReason.transport_error:
            return _SHORT_LINK_TRANSPORT_TEXT
        case _:
            assert_never(reason)


def article_failure_text(marketplace: Marketplace, reason: FetchFailureReason) -> str:
    """Refusal for a bare article, naming where it was looked for.

    ``marketplace`` is where the preview stopped: Ozon means Wildberries
    already answered ``not_found``.
    """
    match marketplace:
        case Marketplace.wb:
            return ARTICLE_WB_FAILURE_TEXTS[reason]
        case Marketplace.ozon:
            return ARTICLE_OZON_FAILURE_TEXTS[reason]
        case _:
            assert_never(marketplace)


def product_found_text(marketplace: Marketplace, name: str, current_price: int) -> str:
    """Product card shown after a successful preview (plain text, no markup)."""
    return (
        f"Нашёл товар на {marketplace_title(marketplace)}:\n{name}\n"
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
        f"Достигнут лимит товаров: {limit}. "
        "Чтобы добавить новый, сначала перестаньте следить за одним из текущих."
    )


def added_text(name: str, target_price: int, direction: AlertDirection) -> str:
    """Tracking added successfully (plain text, no markup)."""
    return (
        f"Отслеживание добавлено: {name}.\n"
        f"Сообщу, когда {direction_phrase(direction, target_price)}.\n"
        f"{PRICE_DISCLAIMER_SHORT}.\n\n" + SOUND_REMINDER_TEXT
    )


def marketplace_title(marketplace: Marketplace) -> str:
    """Human-readable marketplace name (single mapping)."""
    match marketplace:
        case Marketplace.wb:
            return "Wildberries"
        case Marketplace.ozon:
            return "Ozon"
        case _:
            assert_never(marketplace)


def list_text(total: int, page: int, pages: int, price_label: str | None = None) -> str:
    """Header of the product list; names the page when there are several.

    ``price_label`` is the time of the oldest shown price (``price_time_label``).
    """
    head = f"Вы следите за товарами: {total}."
    if pages > 1:
        head = f"{head} Стр. {page}/{pages}."
    prices_line = f"Цены на {price_label}\n" if price_label is not None else ""
    return f"{head}\n{prices_line}{PRICE_DISCLAIMER_SHORT}."


def product_card_text(
    name: str,
    marketplace: Marketplace,
    url: str,
    current_price: int,
    alerts: list[tuple[AlertDirection, int]],
    price_label: str | None = None,
    *,
    initial_line: str | None = None,
) -> str:
    """Product card with its thresholds (plain text, no markup)."""
    when = f" (цена на {price_label})" if price_label is not None else ""
    lines = [
        name,
        f"Маркетплейс: {marketplace_title(marketplace)}",
        url,
        f"Текущая цена: {format_price(current_price)}{when}",
    ]
    if initial_line is not None:
        lines.append(initial_line)
    lines.extend([f"{PRICE_DISCLAIMER_SHORT}.", ""])
    if alerts:
        lines.append("Пороги:")
        lines.extend(
            f"{direction_arrow(direction)} {format_price(price)}"
            for direction, price in alerts
        )
    else:
        lines.append("Порогов нет.")
    return "\n".join(lines)


def remove_alert_button_text() -> str:
    """Label of the button that removes the threshold of its row.

    The amount is carried by the edit button of the same row.
    """
    return "🗑 Убрать"


def edit_alert_button_text(
    direction: AlertDirection, price: int, *, with_arrow: bool = False
) -> str:
    """Label of the button that changes one threshold, amount first.

    The arrow is shown only when another threshold of the same product has the
    same amount, so the label stays unambiguous.
    """
    arrow = f"{direction_arrow(direction)} " if with_arrow else ""
    return f"✏️ {arrow}{format_price(price)}"


# --- changing a threshold from the card ------------------------------------

EDIT_NOT_TEXT_TEXT: Final[str] = (
    "Я понимаю только текст. Пришлите новую сумму в рублях сообщением, "
    "либо нажмите «Отмена»."
)

EDIT_STALE_TEXT: Final[str] = (
    "Это действие устарело. Откройте товар в «Мои товары» и нажмите ✏️ у "
    "нужного порога заново."
)

EDIT_UNCHANGED_TEXT: Final[str] = "Порог не изменился."


def edit_prompt_text(
    name: str, current_price: int, direction: AlertDirection, target_price: int
) -> str:
    """Prompt of the threshold edit step (plain text, no markup)."""
    return (
        f"{name}\n"
        f"Текущая цена: {format_price(current_price)}\n"
        f"{PRICE_DISCLAIMER_SHORT}.\n"
        f"Изменяемый порог: {direction_arrow(direction)} "
        f"{format_price(target_price)}\n\n"
        "Введите новую сумму в рублях, например 1990 или 1990,50. Если она "
        "ниже текущей цены — сообщу, когда цена снизится до неё; если выше — "
        "когда вырастет. Чтобы выйти, нажмите «Отмена»."
    )


def edit_changed_text(direction: AlertDirection | None, target_price: int) -> str:
    """Outcome line of a changed threshold; the direction is spelled out."""
    if direction is None:
        return "Порог изменён."
    return f"Порог изменён. Сообщу, когда {direction_phrase(direction, target_price)}."


# --- manual price refresh ---------------------------------------------------

REFRESH_LIST_BUTTON_TEXT: Final[str] = "🔄 Обновить цены"
REFRESH_CARD_BUTTON_TEXT: Final[str] = "🔄 Обновить цену"
REFRESH_IN_PROGRESS_TEXT: Final[str] = "Обновляю цены…"
REFRESH_GONE_TEXT: Final[str] = "Этот товар больше не отслеживается."
REFRESH_ALL_DONE_TEXT: Final[str] = "Цены обновлены."
REFRESH_CARD_DONE_TEXT: Final[str] = "Цена обновлена."
REFRESH_OLD_PRICE_TEXT: Final[str] = "Показана прежняя цена."

_REFRESH_LIST_LINES: Final[dict[RefreshFailure, str]] = {
    RefreshFailure.blocked: "Маркетплейс заблокировал запрос: {n}",
    RefreshFailure.transport_error: "Маркетплейс не ответил: {n}",
    RefreshFailure.out_of_stock: "Нет в наличии: {n}",
    RefreshFailure.not_found: "Не найдено на маркетплейсе: {n}",
    RefreshFailure.bad_payload: "Маркетплейс ответил в неожиданном виде: {n}",
    RefreshFailure.timed_out: "Не успели: {n}",
    RefreshFailure.error: "Не удалось сохранить цену: {n}",
}

_REFRESH_CARD_REASONS: Final[dict[RefreshFailure, str]] = {
    RefreshFailure.blocked: (
        "заблокировал запрос: он защищается от автоматических обращений."
    ),
    RefreshFailure.transport_error: "Маркетплейс не ответил.",
    RefreshFailure.out_of_stock: "Товара сейчас нет в наличии.",
    RefreshFailure.not_found: "Товар на маркетплейсе не нашёлся.",
    RefreshFailure.bad_payload: "Маркетплейс ответил в неожиданном виде.",
    RefreshFailure.timed_out: "Не успели получить ответ вовремя.",
    RefreshFailure.error: "Не удалось сохранить новую цену.",
}


def refresh_throttled_text(retry_after_seconds: int) -> str:
    """Popup shown when the once-a-minute refresh budget is spent."""
    return (
        f"Обновлять цены можно раз в минуту. Попробуйте через {retry_after_seconds} с."
    )


def price_time_label(fetched_at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """Time of a price in the display zone; the date is added if not today there."""
    local = fetched_at.astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return f"{local:%H:%M}"
    return f"{local:%d.%m %H:%M}"


def added_date_label(created_at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """Date a product was added in the display zone; the year only if not current."""
    local = created_at.astimezone(tz)
    if local.year == now.astimezone(tz).year:
        return f"{local:%d.%m}"
    return f"{local:%d.%m.%Y}"


def initial_price_text(initial_price: int, current_price: int, added_label: str) -> str:
    """Card line with the price at the time of adding and the change since then."""
    head = f"При добавлении: {format_price(initial_price)} ({added_label}) · "
    delta = current_price - initial_price
    if delta == 0:
        return f"{head}без изменений"
    sign = "+" if delta > 0 else "\u2212"
    money = f"{sign}{format_price(abs(delta))}"
    if initial_price == 0:
        return f"{head}{money}"
    # Tenths of a percent, rounded half up, in integers (no float error).
    tenths = (2 * abs(delta) * 1000 + initial_price) // (2 * initial_price)
    if tenths == 0:
        percent = "менее 0,1\u00a0%"
    else:
        percent = f"{sign}{tenths // 10},{tenths % 10}\u00a0%"
    return f"{head}{money} ({percent})"


def refresh_list_summary(report: RefreshReport) -> str:
    """Summary line(s) above the list after a manual refresh."""
    if report.updated == report.total:
        return REFRESH_ALL_DONE_TEXT
    lines = [f"Обновлено {report.updated} из {report.total}."]
    lines.extend(
        template.format(n=report.failures[reason])
        for reason, template in _REFRESH_LIST_LINES.items()
        if report.failures.get(reason)
    )
    return "\n".join(lines)


def refresh_card_summary(report: RefreshReport, marketplace: Marketplace) -> str:
    """Summary above the card after a manual refresh of its single product."""
    if report.updated == report.total:
        return REFRESH_CARD_DONE_TEXT
    reason = next(iter(report.failures), RefreshFailure.error)
    phrase = _REFRESH_CARD_REASONS[reason]
    if reason is RefreshFailure.blocked:
        phrase = f"{marketplace_title(marketplace)} {phrase}"
    return f"{phrase} {REFRESH_OLD_PRICE_TEXT}"
