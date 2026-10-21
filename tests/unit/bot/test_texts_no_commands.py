"""Guard: user-facing texts point at buttons, not at slash commands (PAB-098 AC5).

Every string constant of ``app.bot.texts`` and the output of every public text
function (on sample arguments) is scanned for ``/start``, ``/add``, ``/list``,
``/help`` and ``/cancel``. The only place that may name commands is the
"Команды:" section of ``help_text``. Literal expectations of the changed texts
are written out by hand.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable

import pytest
from aiogram.types import BotCommand

from app.bot import texts
from app.models.enums import AlertDirection
from app.models.enums import Marketplace

# A slash that is not part of a URL or path (no word char, "/" or ":" before it)
# followed by a bot command name up to a word boundary.
_COMMAND = re.compile(r"(?<![\w/:])/(?:start|add|list|help|cancel)\b")
_COMMANDS_SECTION = "Команды:"

_SAMPLE_FUNCTIONS: dict[str, Callable[[], str]] = {
    "help_text": lambda: texts.help_text(50).split(_COMMANDS_SECTION)[0],
    "direction_phrase": lambda: texts.direction_phrase(AlertDirection.below, 1_000_00),
    "direction_arrow": lambda: texts.direction_arrow(AlertDirection.above),
    "add_ref_offer_text": lambda: texts.add_ref_offer_text(Marketplace.wb, 12345678),
    "product_found_text": lambda: texts.product_found_text("Item", 1_990_00),
    "confirm_text": lambda: texts.confirm_text(
        "Item", 1_990_00, 1_000_00, AlertDirection.below
    ),
    "limit_exceeded_text": lambda: texts.limit_exceeded_text(50),
    "added_text": lambda: texts.added_text("Item", 1_000_00, AlertDirection.above),
    "marketplace_title": lambda: texts.marketplace_title(Marketplace.ozon),
    "list_text": lambda: texts.list_text(50, 60),
    "product_card_text": lambda: texts.product_card_text(
        "Item",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00), (AlertDirection.above, 2_000_00)],
    ),
    "remove_alert_button_text": texts.remove_alert_button_text,
    "edit_alert_button_text": lambda: texts.edit_alert_button_text(
        AlertDirection.below, 1_000_00, with_arrow=True
    ),
    "edit_prompt_text": lambda: texts.edit_prompt_text(
        "Item", 1_990_00, AlertDirection.below, 1_000_00
    ),
    "edit_changed_text": lambda: texts.edit_changed_text(
        AlertDirection.below, 1_000_00
    ),
}


def _public_text_functions() -> set[str]:
    return {
        name
        for name, obj in vars(texts).items()
        if inspect.isfunction(obj)
        and obj.__module__ == texts.__name__
        and not name.startswith("_")
    }


def _flatten(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, BotCommand):
        return [value.description]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _flatten(item)]
    if isinstance(value, list):
        return [s for item in value for s in _flatten(item)]
    return []


def _constant_strings() -> dict[str, list[str]]:
    return {
        name: strings
        for name, value in vars(texts).items()
        if name.isupper() and (strings := _flatten(value))
    }


def _has_command(text: str) -> bool:
    return _COMMAND.search(text) is not None


def test_detector_catches_commands_and_ignores_urls_and_paths() -> None:
    for command in ("/start", "/add", "/list", "/help", "/cancel"):
        assert _has_command(f"Откройте меню командой {command}.")
        assert _has_command(f"(см. {command})")
        assert _has_command(command)
    assert not _has_command("https://www.wildberries.ru/catalog/1/detail.aspx")
    assert not _has_command("https://www.ozon.ru/product/add/list/1/")
    assert not _has_command("ozon.ru/add и wildberries.ru/list")
    assert not _has_command("Добавить/добавить, start/stop")


def test_every_public_text_function_has_a_sample_row() -> None:
    assert _public_text_functions() == set(_SAMPLE_FUNCTIONS)


def test_constants_table_is_not_empty_and_includes_the_changed_ones() -> None:
    names = set(_constant_strings())

    assert {"MENU_STALE_ALERT_TEXT", "ADD_STALE_TEXT", "EDIT_STALE_TEXT"} <= names
    assert "BOT_COMMANDS" in names
    assert "ADD_FETCH_FAILURE_TEXTS" in names


@pytest.mark.parametrize("name", sorted(_constant_strings()))
def test_text_constant_names_no_slash_command(name: str) -> None:
    for text in _constant_strings()[name]:
        assert not _has_command(text), f"{name}: {text!r}"


@pytest.mark.parametrize("name", sorted(_SAMPLE_FUNCTIONS))
def test_text_function_output_names_no_slash_command(name: str) -> None:
    text = _SAMPLE_FUNCTIONS[name]()

    assert text
    assert not _has_command(text), f"{name}: {text!r}"


def test_help_text_names_commands_only_in_its_commands_section() -> None:
    head, _, tail = texts.help_text(50).partition(_COMMANDS_SECTION)

    assert not _has_command(head)
    for command in ("/start", "/add", "/list", "/help", "/cancel"):
        assert command in tail


def test_help_text_prose_points_at_the_buttons() -> None:
    head = texts.help_text(50).split(_COMMANDS_SECTION)[0]

    assert "Нажмите «➕ Добавить товар»" in head
    assert "видны в «Мои товары»" in head


# --- literal wordings of PAB-098 (Б) -----------------------------------------


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        (
            texts.MENU_STALE_ALERT_TEXT,
            "Кнопка устарела. Воспользуйтесь кнопками последнего сообщения бота.",
        ),
        (
            texts.ADD_STALE_ALERT_TEXT,
            "Кнопка устарела. Чтобы добавить товар, нажмите «➕ Добавить товар» "
            "в главном меню.",
        ),
        (
            texts.LIST_STALE_BUTTON_ALERT_TEXT,
            "Кнопка устарела. Откройте актуальный список: «Главное меню» → "
            "«Мои товары».",
        ),
        (
            texts.LIST_GONE_ALERT_TEXT,
            "Товар или порог уже не отслеживается. Откройте актуальный список: "
            "«Главное меню» → «Мои товары».",
        ),
        (
            texts.ADD_PROMPT_LINK_TEXT,
            "Пришлите ссылку на товар Wildberries или Ozon либо артикул "
            "Wildberries (только цифры). Чтобы выйти, нажмите «Отмена».",
        ),
        (
            texts.ADD_TARGET_EQUALS_RESTART_TEXT,
            "Порог совпал с текущей ценой, поэтому отслеживание не добавлено. "
            "Начните заново кнопкой «➕ Добавить товар» и укажите цену ниже или "
            "выше текущей.",
        ),
        (
            texts.ADD_STALE_TEXT,
            "Это действие устарело. Начните заново кнопкой «➕ Добавить товар».",
        ),
        (
            texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
            "Порог удалён. Других порогов у товара не было, поэтому я перестал "
            "за ним следить. Чтобы вернуть товар, добавьте его заново: "
            "«Главное меню» → «➕ Добавить товар».",
        ),
        (
            texts.LIST_PRODUCT_REMOVED_TEXT,
            "Больше не слежу за этим товаром, его пороги удалены. Чтобы вернуть "
            "товар, добавьте его заново: «Главное меню» → «➕ Добавить товар».",
        ),
        (
            texts.EDIT_STALE_TEXT,
            "Это действие устарело. Откройте товар в «Мои товары» и нажмите ✏️ "
            "у нужного порога заново.",
        ),
        (
            texts.ALERT_DUPLICATE_TEXT,
            "У вас уже есть такой порог. Введите другую сумму в рублях, "
            "например 1990 или 1990,50.",
        ),
        (texts.LIST_BACK_TO_LIST_BUTTON_TEXT, "← К списку товаров"),
    ],
)
def test_changed_text_is_the_agreed_wording(actual: str, expected: str) -> None:
    assert actual == expected


def test_replaced_duplicate_constants_are_gone() -> None:
    assert not hasattr(texts, "ADD_DUPLICATE_TEXT")
    assert not hasattr(texts, "EDIT_DUPLICATE_TEXT")
