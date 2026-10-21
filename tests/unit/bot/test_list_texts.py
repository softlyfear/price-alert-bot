"""Tests for the /list texts of app.bot.texts (PAB-070 AC7, AC8, AC10).

Expected strings are written out by hand (with U+00A0 inside prices), not
rebuilt from the same helpers the code uses.
"""

from typing import cast

import pytest

from app.bot import texts
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.models.enums import AlertDirection
from app.models.enums import Marketplace

_NB = "\u00a0"
_DISCLAIMER = "Цена без учёта персональных скидок"


def test_list_text_without_truncation_names_the_total_and_has_no_cut_note() -> None:
    assert texts.list_text(3, 3) == (
        "Вы следите за товарами: 3.\n"
        "Выберите товар, чтобы посмотреть пороги или перестать следить.\n"
        f"{_DISCLAIMER}."
    )


def test_list_text_with_truncation_says_first_50_of_51() -> None:
    text = texts.list_text(50, 51)

    assert "Показаны первые 50 из 51." in text
    assert text.startswith("Вы следите за товарами: 51.")


def test_list_text_carries_the_short_disclaimer_exactly_once() -> None:
    assert texts.list_text(2, 2).count(PRICE_DISCLAIMER_SHORT) == 1
    assert texts.list_text(50, 60).count(PRICE_DISCLAIMER_SHORT) == 1


def test_marketplace_title_is_the_human_name() -> None:
    assert texts.marketplace_title(Marketplace.wb) == "Wildberries"
    assert texts.marketplace_title(Marketplace.ozon) == "Ozon"


def test_product_card_text_shows_short_arrow_thresholds_and_one_legend() -> None:
    text = texts.product_card_text(
        "Кроссовки",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00), (AlertDirection.above, 2_000_00)],
    )

    assert text == (
        "Кроссовки\n"
        "Маркетплейс: Wildberries\n"
        "https://www.wildberries.ru/catalog/1/detail.aspx\n"
        f"Текущая цена: 1{_NB}990{_NB}₽\n"
        f"{_DISCLAIMER}.\n"
        "\n"
        "Пороги:\n"
        f"• ↓ 1{_NB}000{_NB}₽\n"
        f"• ↑ 2{_NB}000{_NB}₽\n"
        "\n"
        "↓ — сообщу, когда цена снизится до порога\n"
        "↑ — сообщу, когда вырастет до него"
    )
    assert "цена станет" not in text
    assert text.count("↓ — сообщу") == 1


def test_product_card_text_without_thresholds_says_there_are_none() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.ozon, "https://www.ozon.ru/product/1/", 100, []
    )

    assert text.endswith("Порогов нет.")
    assert "Пороги:" not in text
    assert "↓" not in text
    assert "↑" not in text
    assert "Маркетплейс: Ozon" in text


def test_remove_alert_button_text_is_the_fixed_label_without_an_amount() -> None:
    assert texts.remove_alert_button_text() == "🗑 Убрать"


def test_edit_alert_button_text_puts_the_amount_first_without_arrow_by_default() -> (
    None
):
    assert texts.edit_alert_button_text(AlertDirection.above, 3_000_00) == (
        f"✏️ 3{_NB}000{_NB}₽"
    )


@pytest.mark.parametrize(
    ("direction", "expected"),
    [(AlertDirection.below, "↓"), (AlertDirection.above, "↑")],
)
def test_edit_alert_button_text_with_arrow_puts_it_before_the_amount(
    direction: AlertDirection, expected: str
) -> None:
    assert texts.edit_alert_button_text(direction, 1_000_00, with_arrow=True) == (
        f"✏️ {expected} 1{_NB}000{_NB}₽"
    )


def test_card_text_with_thresholds_has_a_blank_line_before_the_two_line_legend() -> (
    None
):
    lines = texts.product_card_text(
        "Item",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00)],
    ).split("\n")

    assert lines[-4:] == [
        f"• ↓ 1{_NB}000{_NB}₽",
        "",
        "↓ — сообщу, когда цена снизится до порога",
        "↑ — сообщу, когда вырастет до него",
    ]


def test_card_text_without_thresholds_has_no_legend_and_no_blank_tail() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.wb, "https://x.example/1", 1_990_00, []
    )

    assert text.endswith(f"{_DISCLAIMER}.\n\nПорогов нет.")
    assert "сообщу" not in text


def test_direction_arrow_rejects_a_value_outside_the_enum() -> None:
    with pytest.raises(AssertionError):
        texts.direction_arrow(cast(AlertDirection, "sideways"))


@pytest.mark.parametrize(
    ("direction", "phrase"),
    [
        (AlertDirection.below, f"Сообщу, когда цена станет ниже 1{_NB}000{_NB}₽"),
        (AlertDirection.above, f"Сообщу, когда цена станет выше 1{_NB}000{_NB}₽"),
    ],
)
def test_confirm_and_added_texts_keep_the_full_direction_phrase(
    direction: AlertDirection, phrase: str
) -> None:
    confirm = texts.confirm_text("Item", 2_000_00, 1_000_00, direction)
    added = texts.added_text("Item", 1_000_00, direction)

    assert phrase in confirm
    assert phrase in added


def test_gone_and_stale_alerts_are_distinct_and_not_about_previous_state() -> None:
    assert texts.LIST_GONE_ALERT_TEXT != texts.LIST_STALE_BUTTON_ALERT_TEXT
    for text in (texts.LIST_GONE_ALERT_TEXT, texts.LIST_STALE_BUTTON_ALERT_TEXT):
        assert "отключ" not in text
        assert "был активен" not in text
    assert "уже не отслеживается" in texts.LIST_GONE_ALERT_TEXT


def test_removal_outcome_texts_are_distinct_and_removed_ones_point_to_add() -> None:
    outcomes = {
        texts.ALERT_REMOVED_TEXT,
        texts.ALERT_ALREADY_REMOVED_TEXT,
        texts.PRODUCT_ALREADY_GONE_TEXT,
        texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
        texts.LIST_PRODUCT_REMOVED_TEXT,
        texts.LIST_GONE_ALERT_TEXT,
    }

    assert len(outcomes) == 6
    for removed in (
        texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
        texts.LIST_PRODUCT_REMOVED_TEXT,
    ):
        assert removed.endswith(
            "Чтобы вернуть товар, добавьте его заново: "
            "«Главное меню» → «➕ Добавить товар»."
        )
    assert "➕ Добавить товар" in texts.LIST_EMPTY_TEXT


def test_short_removal_texts_are_the_literal_phrases() -> None:
    assert texts.ALERT_REMOVED_TEXT == "Порог удалён"
    assert texts.ALERT_ALREADY_REMOVED_TEXT == "Порог уже удалён"
    assert texts.PRODUCT_ALREADY_GONE_TEXT == "Товар уже не отслеживается"


def test_marketplace_title_rejects_a_value_outside_the_enum() -> None:
    with pytest.raises(AssertionError):
        texts.marketplace_title(cast(Marketplace, "amazon"))
