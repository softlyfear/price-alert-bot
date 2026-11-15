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
from app.models.enums import ProductFailure

_NB = "\u00a0"
_DISCLAIMER = "Цена без учёта персональных скидок"


def test_list_text_for_one_page_has_no_page_marker() -> None:
    assert texts.list_text(3, 1, 1) == (f"Вы следите за товарами: 3.\n{_DISCLAIMER}.")


def test_list_text_for_several_pages_names_the_page_exactly() -> None:
    assert texts.list_text(23, 3, 3) == (
        f"Вы следите за товарами: 23. Стр. 3/3.\n{_DISCLAIMER}."
    )


def test_list_text_never_says_first_n_of_m() -> None:
    assert "Показаны первые" not in texts.list_text(60, 1, 6)


def test_list_text_carries_the_short_disclaimer_exactly_once() -> None:
    assert texts.list_text(2, 1, 1).count(PRICE_DISCLAIMER_SHORT) == 1
    assert texts.list_text(60, 2, 6).count(PRICE_DISCLAIMER_SHORT) == 1


def test_marketplace_title_is_the_human_name() -> None:
    assert texts.marketplace_title(Marketplace.wb) == "Wildberries"
    assert texts.marketplace_title(Marketplace.ozon) == "Ozon"


def test_list_text_with_pages_and_a_label_has_all_three_parts() -> None:
    text = texts.list_text(51, 2, 6, "14:05")

    assert text == (
        "Вы следите за товарами: 51. Стр. 2/6.\n"
        "Цены на 14:05\n"
        "Цена без учёта персональных скидок."
    )
    assert text.count(PRICE_DISCLAIMER_SHORT) == 1


def test_list_text_with_a_label_has_one_disclaimer_and_no_select_hint() -> None:
    text = texts.list_text(3, 1, 1, "14:05")

    assert text == (
        "Вы следите за товарами: 3.\nЦены на 14:05\nЦена без учёта персональных скидок."
    )
    assert text.count(PRICE_DISCLAIMER_SHORT) == 1


def test_product_card_text_lists_thresholds_with_chart_markers_and_no_legend() -> None:
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
        f"📉 1{_NB}000{_NB}₽\n"
        f"📈 2{_NB}000{_NB}₽"
    )
    assert "цена станет" not in text
    assert "•" not in text
    assert "сообщу, когда" not in text


def test_product_card_text_without_thresholds_says_there_are_none() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.ozon, "https://www.ozon.ru/product/1/", 100, []
    )

    assert text.endswith("Порогов нет.")
    assert "Пороги:" not in text
    assert "📉" not in text
    assert "📈" not in text
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
    [(AlertDirection.below, "📉"), (AlertDirection.above, "📈")],
)
def test_edit_alert_button_text_with_arrow_puts_it_before_the_amount(
    direction: AlertDirection, expected: str
) -> None:
    assert texts.edit_alert_button_text(direction, 1_000_00, with_arrow=True) == (
        f"✏️ {expected} 1{_NB}000{_NB}₽"
    )


def test_card_text_with_thresholds_ends_with_the_last_threshold_line() -> None:
    lines = texts.product_card_text(
        "Item",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00)],
    ).split("\n")

    assert lines[-2:] == ["Пороги:", f"📉 1{_NB}000{_NB}₽"]


def test_direction_arrow_uses_chart_emoji_without_variation_selector() -> None:
    assert texts.direction_arrow(AlertDirection.below) == "\U0001f4c9"
    assert texts.direction_arrow(AlertDirection.above) == "\U0001f4c8"


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
    ("failure", "status"),
    [
        (ProductFailure.out_of_stock, "Сейчас нет в наличии."),
        (ProductFailure.not_found, "Товар не найден на маркетплейсе."),
        (ProductFailure.blocked, "Не удалось проверить цену."),
        (ProductFailure.transport_error, "Не удалось проверить цену."),
        (ProductFailure.bad_payload, "Не удалось проверить цену."),
    ],
)
def test_availability_status_text_maps_each_failure(
    failure: ProductFailure, status: str
) -> None:
    assert texts.availability_status_text(failure) == status


def test_availability_status_text_is_empty_without_a_failure() -> None:
    assert texts.availability_status_text(None) is None


@pytest.mark.parametrize(
    ("failure", "short"),
    [
        (ProductFailure.out_of_stock, "Нет в наличии"),
        (ProductFailure.not_found, "Не найден"),
        (ProductFailure.blocked, "Не проверено"),
        (ProductFailure.transport_error, "Не проверено"),
        (ProductFailure.bad_payload, "Не проверено"),
    ],
)
def test_availability_short_text_maps_each_failure(
    failure: ProductFailure, short: str
) -> None:
    assert texts.availability_short_text(failure) == short


def test_availability_texts_reject_a_value_outside_the_enum() -> None:
    bogus = cast(ProductFailure, "gone")
    with pytest.raises(AssertionError):
        texts.availability_status_text(bogus)
    with pytest.raises(AssertionError):
        texts.availability_short_text(bogus)


def test_card_of_an_unavailable_product_labels_the_stale_price_and_status() -> None:
    """PAB-135 Р1: the price of a failed check is never presented as current."""
    text = texts.product_card_text(
        "Кроссовки",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00)],
        "14:05",
        last_failure=ProductFailure.out_of_stock,
    )

    assert f"Последняя известная цена: 1{_NB}990{_NB}₽ (цена на 14:05)" in text
    assert "Сейчас нет в наличии." in text
    assert "Текущая цена" not in text
    assert text.index("Последняя известная цена") < text.index("Сейчас нет в наличии.")


def test_card_without_a_known_price_never_shows_zero_rubles() -> None:
    """PAB-135 Р1: a product added without a price has no fabricated amount."""
    text = texts.product_card_text(
        "Кроссовки",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        0,
        [],
        last_failure=ProductFailure.out_of_stock,
    )

    assert "Цена пока неизвестна." in text
    assert "0" not in text.replace("catalog/1", "")
    assert "Сейчас нет в наличии." in text
    assert "Текущая цена" not in text


def test_card_without_a_known_price_and_without_a_failure_still_says_unknown() -> None:
    text = texts.product_card_text(
        "Кроссовки", Marketplace.wb, "https://x.example/1", 0, []
    )

    assert "Цена пока неизвестна." in text
    assert "Сейчас нет в наличии." not in text
    assert "Не удалось проверить цену." not in text


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
