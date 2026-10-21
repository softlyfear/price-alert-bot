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
from app.models.enums import Marketplace
from app.schemas.marketplace import FetchFailureReason

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

FALLBACK_TEXT: Final[str] = (
    "Не понял сообщение. Выберите действие ниже или пришлите ссылку на "
    "товар Wildberries или Ozon, либо артикул Wildberries."
)

ADD_REF_BUTTON_TEXT: Final[str] = "➕ Добавить этот товар"

MENU_STALE_ALERT_TEXT: Final[str] = "Кнопка устарела. Откройте меню командой /start."


def help_text(max_products: int) -> str:
    """Detailed help; the product limit comes from the settings."""
    return (
        "Я слежу за ценами на Wildberries и Ozon и пишу вам, когда цена "
        "товара доходит до нужного вам порога.\n\n"
        "Как добавить товар. Отправьте команду /add и пришлите ссылку на "
        "товар Wildberries или Ozon. Артикул без ссылки я понимаю только "
        "для Wildberries. Я покажу найденный товар и его текущую цену, "
        "затем попрошу указать порог в рублях.\n\n"
        "Как работает порог. Если порог ниже текущей цены, я сообщу, когда "
        "цена снизится до него. Если выше — когда цена вырастет до него. "
        "Порог, равный текущей цене, не подойдёт. Сработав, порог остаётся "
        "активным: я напишу снова, если цена опять окажется за порогом, но "
        "не чаще, чем раз в установленный интервал.\n\n"
        f"Лимит товаров: {max_products}.\n\n"
        "Свои товары и пороги видны в /list. Там же, в карточке товара, можно "
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
    "Пришлите ссылку на товар Wildberries или Ozon либо артикул Wildberries "
    "(только цифры). Чтобы выйти, нажмите «Отмена» или отправьте /cancel."
)

ADD_CHECKING_TEXT: Final[str] = (
    "Проверяю товар — это может занять некоторое время, пожалуйста, подождите."
)

ADD_CHECK_DROPPED_TEXT: Final[str] = "Проверка прервана."

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

# --- /list: products, card, removal ----------------------------------------

LIST_EMPTY_TEXT: Final[str] = (
    "Вы пока ни за чем не следите. Нажмите «➕ Добавить товар» ниже, "
    "чтобы добавить первый."
)

LIST_STALE_BUTTON_ALERT_TEXT: Final[str] = (
    "Кнопка устарела. Откройте актуальный список командой /list."
)

LIST_GONE_ALERT_TEXT: Final[str] = (
    "Товар или порог уже не отслеживается. Откройте актуальный список командой /list."
)

ALERT_REMOVED_TEXT: Final[str] = "Порог удалён"

ALERT_ALREADY_REMOVED_TEXT: Final[str] = "Порог уже удалён"

PRODUCT_ALREADY_GONE_TEXT: Final[str] = "Товар уже не отслеживается"

ADD_ALREADY_TRACKED_TEXT: Final[str] = "Товар уже отслеживается"

LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT: Final[str] = (
    "Порог удалён. Других порогов у товара не было, поэтому я перестал за ним "
    "следить. Чтобы вернуть товар, добавьте его заново командой /add."
)

LIST_PRODUCT_REMOVED_TEXT: Final[str] = (
    "Больше не слежу за этим товаром, его пороги удалены. Чтобы вернуть "
    "товар, добавьте его заново командой /add."
)

ADD_ALERT_BUTTON_TEXT: Final[str] = "➕ Добавить порог"

STOP_TRACKING_BUTTON_TEXT: Final[str] = "Перестать следить"
LIST_BACK_BUTTON_TEXT: Final[str] = "← К списку"

CARD_ARROWS_LEGEND_TEXT: Final[str] = (
    "↓ — сообщу, когда цена снизится до порога, ↑ — когда вырастет до него."
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


def direction_arrow(direction: AlertDirection) -> str:
    """Short arrow of a direction used on the product card (single mapping)."""
    match direction:
        case AlertDirection.below:
            return "↓"
        case AlertDirection.above:
            return "↑"
        case _:
            assert_never(direction)


def add_ref_offer_text(marketplace: Marketplace, article: int) -> str:
    """Offer to add a product recognised in free text (plain text, no markup)."""
    return f"Добавить этот товар? {marketplace_title(marketplace)}, артикул {article}."


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


def list_text(shown: int, total: int) -> str:
    """Header of the product list; names the truncation when buttons are cut."""
    head = f"Вы следите за товарами: {total}."
    if shown < total:
        head = f"{head} Показаны первые {shown} из {total}."
    return (
        f"{head}\nВыберите товар, чтобы посмотреть пороги или перестать следить.\n"
        f"{PRICE_DISCLAIMER_SHORT}."
    )


def product_card_text(
    name: str,
    marketplace: Marketplace,
    url: str,
    current_price: int,
    alerts: list[tuple[AlertDirection, int]],
) -> str:
    """Product card with its thresholds (plain text, no markup)."""
    lines = [
        name,
        f"Маркетплейс: {marketplace_title(marketplace)}",
        url,
        f"Текущая цена: {format_price(current_price)}",
        f"{PRICE_DISCLAIMER_SHORT}.",
        "",
    ]
    if alerts:
        lines.append("Пороги:")
        lines.extend(
            f"• {direction_arrow(direction)} {format_price(price)}"
            for direction, price in alerts
        )
        lines.append(CARD_ARROWS_LEGEND_TEXT)
    else:
        lines.append("Порогов нет.")
    return "\n".join(lines)


def remove_alert_button_text(
    direction: AlertDirection, price: int, *, with_arrow: bool = False
) -> str:
    """Label of the button that removes one threshold.

    The arrow is shown only when another threshold of the same product has the
    same amount, so the label stays unambiguous.
    """
    arrow = f"{direction_arrow(direction)} " if with_arrow else ""
    return f"Убрать порог: {arrow}{format_price(price)}"


def edit_alert_button_text(
    direction: AlertDirection, price: int, *, with_arrow: bool = False
) -> str:
    """Label of the button that changes one threshold (arrow rule as for removal)."""
    arrow = f"{direction_arrow(direction)} " if with_arrow else ""
    return f"Изменить порог: {arrow}{format_price(price)}"


# --- changing a threshold from the card ------------------------------------

EDIT_DUPLICATE_TEXT: Final[str] = (
    "Такой порог у этого товара уже есть. Введите другую сумму либо нажмите «Отмена»."
)

EDIT_NOT_TEXT_TEXT: Final[str] = (
    "Я понимаю только текст. Пришлите новую сумму в рублях сообщением, "
    "либо нажмите «Отмена»."
)

EDIT_STALE_TEXT: Final[str] = (
    "Это действие устарело. Откройте товар в /list и нажмите «Изменить порог» заново."
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
