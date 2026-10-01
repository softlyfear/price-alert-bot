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


def test_product_card_text_lists_every_threshold_with_its_direction() -> None:
    text = texts.product_card_text(
        "Кроссовки",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_500_00), (AlertDirection.above, 2_500_50)],
    )

    assert text == (
        "Кроссовки\n"
        "Маркетплейс: Wildberries\n"
        "https://www.wildberries.ru/catalog/1/detail.aspx\n"
        f"Текущая цена: 1{_NB}990{_NB}₽\n"
        f"{_DISCLAIMER}.\n"
        "\n"
        "Пороги:\n"
        f"• Сообщу, когда цена станет ниже 1{_NB}500{_NB}₽\n"
        f"• Сообщу, когда цена станет выше 2{_NB}500,50{_NB}₽"
    )


def test_product_card_text_without_thresholds_says_there_are_none() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.ozon, "https://www.ozon.ru/product/1/", 100, []
    )

    assert text.endswith("Порогов нет.")
    assert "Пороги:" not in text
    assert "Маркетплейс: Ozon" in text


def test_remove_alert_button_text_names_the_condition() -> None:
    assert texts.remove_alert_button_text(AlertDirection.above, 3_000_00) == (
        f"Убрать порог: цена станет выше 3{_NB}000{_NB}₽"
    )


def test_gone_and_stale_alerts_are_distinct_and_not_about_previous_state() -> None:
    assert texts.LIST_GONE_ALERT_TEXT != texts.LIST_STALE_BUTTON_ALERT_TEXT
    for text in (texts.LIST_GONE_ALERT_TEXT, texts.LIST_STALE_BUTTON_ALERT_TEXT):
        assert "отключ" not in text
        assert "был активен" not in text
    assert "уже не отслеживается" in texts.LIST_GONE_ALERT_TEXT


def test_removal_outcome_texts_are_distinct_and_removed_ones_point_to_add() -> None:
    outcomes = {
        texts.LIST_ALERT_REMOVED_TEXT,
        texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
        texts.LIST_PRODUCT_REMOVED_TEXT,
        texts.LIST_GONE_ALERT_TEXT,
    }

    assert len(outcomes) == 4
    assert "/add" in texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT
    assert "/add" in texts.LIST_PRODUCT_REMOVED_TEXT
    assert "/add" in texts.LIST_EMPTY_TEXT


def test_marketplace_title_rejects_a_value_outside_the_enum() -> None:
    with pytest.raises(AssertionError):
        texts.marketplace_title(cast(Marketplace, "amazon"))
