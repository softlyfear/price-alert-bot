"""Tests for the manual-refresh texts of app.bot.texts (PAB-100 AC8, AC9).

Expected strings are written out by hand (U+00A0 inside prices), not rebuilt
from the helpers the code uses. The clock is an argument of
``price_time_label``, so nothing here depends on the real time.
"""

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot import texts
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.services.price_service import RefreshFailure
from app.services.price_service import RefreshReport

_NB = "\u00a0"
_MOSCOW = ZoneInfo("Europe/Moscow")  # UTC+3, no daylight saving


# --- price_time_label -------------------------------------------------------


def test_label_of_a_price_fetched_today_is_the_time_only() -> None:
    fetched = datetime(2026, 10, 22, 11, 5, tzinfo=UTC)
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "14:05"


def test_label_of_a_price_fetched_yesterday_carries_the_date() -> None:
    fetched = datetime(2026, 10, 21, 11, 5, tzinfo=UTC)
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "21.10 14:05"


def test_label_zero_pads_day_month_hour_and_minute() -> None:
    fetched = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    now = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "02.01 06:04"


def test_label_compares_dates_in_the_display_zone_when_utc_dates_are_equal() -> None:
    # Same UTC day (22.10), but 23:30 Moscow on 22.10 vs 00:30 Moscow on 23.10.
    fetched = datetime(2026, 10, 22, 20, 30, tzinfo=UTC)
    now = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "22.10 23:30"


def test_label_compares_dates_in_the_display_zone_when_utc_dates_differ() -> None:
    # 22:30 UTC is 01:30 on the 23rd in Moscow; now is 03:10 on the 23rd there.
    fetched = datetime(2026, 10, 22, 22, 30, tzinfo=UTC)
    now = datetime(2026, 10, 23, 0, 10, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "01:30"


def test_label_follows_the_given_zone() -> None:
    fetched = datetime(2026, 10, 22, 11, 5, tzinfo=UTC)
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    # Asia/Novosibirsk is UTC+7.
    assert texts.price_time_label(fetched, now, ZoneInfo("Asia/Novosibirsk")) == "18:05"


def test_label_of_a_price_from_the_future_is_still_formatted() -> None:
    fetched = datetime(2026, 10, 23, 11, 5, tzinfo=UTC)
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    assert texts.price_time_label(fetched, now, _MOSCOW) == "23.10 14:05"


# --- list and card headers --------------------------------------------------


def test_list_text_with_a_price_label_adds_one_line_above_the_disclaimer() -> None:
    assert texts.list_text(3, 3, "14:05") == (
        "Вы следите за товарами: 3.\n"
        "Выберите товар, чтобы посмотреть пороги или перестать следить.\n"
        "Цены на 14:05\n"
        "Цена без учёта персональных скидок."
    )


def test_list_text_with_a_dated_price_label_keeps_the_date() -> None:
    assert "\nЦены на 21.10 14:05\n" in texts.list_text(3, 3, "21.10 14:05")


def test_list_text_without_a_price_label_has_no_prices_line() -> None:
    assert "Цены на" not in texts.list_text(3, 3, None)


def test_card_text_with_a_price_label_puts_it_in_brackets_after_the_price() -> None:
    text = texts.product_card_text(
        "Кроссовки",
        Marketplace.wb,
        "https://www.wildberries.ru/catalog/1/detail.aspx",
        1_990_00,
        [(AlertDirection.below, 1_000_00)],
        "14:05",
    )

    assert f"\nТекущая цена: 1{_NB}990{_NB}₽ (цена на 14:05)\n" in text


def test_card_text_without_a_price_label_has_no_brackets() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.ozon, "https://www.ozon.ru/product/1/", 100, [], None
    )

    assert f"Текущая цена: 1{_NB}₽\n" in text
    assert "цена на" not in text


# --- popup and summaries ----------------------------------------------------


def test_throttled_popup_names_the_seconds() -> None:
    assert texts.refresh_throttled_text(37) == (
        "Обновлять цены можно раз в минуту. Попробуйте через 37 с."
    )


def test_list_summary_of_a_full_success_is_one_short_line() -> None:
    assert texts.refresh_list_summary(RefreshReport(3, 3, {})) == "Цены обновлены."


@pytest.mark.parametrize(
    ("reason", "line"),
    [
        (RefreshFailure.blocked, "Маркетплейс заблокировал запрос: 2"),
        (RefreshFailure.transport_error, "Маркетплейс не ответил: 2"),
        (RefreshFailure.out_of_stock, "Нет в наличии: 2"),
        (RefreshFailure.not_found, "Не найдено на маркетплейсе: 2"),
        (RefreshFailure.bad_payload, "Маркетплейс ответил в неожиданном виде: 2"),
        (RefreshFailure.timed_out, "Не успели: 2"),
        (RefreshFailure.error, "Не удалось сохранить цену: 2"),
    ],
)
def test_list_summary_names_the_count_and_the_one_failed_category(
    reason: RefreshFailure, line: str
) -> None:
    summary = texts.refresh_list_summary(RefreshReport(5, 3, {reason: 2}))

    assert summary == f"Обновлено 3 из 5.\n{line}"


def test_list_summary_lists_only_the_non_zero_categories_in_a_fixed_order() -> None:
    report = RefreshReport(
        10,
        4,
        {
            RefreshFailure.timed_out: 1,
            RefreshFailure.blocked: 3,
            RefreshFailure.not_found: 2,
        },
    )

    assert texts.refresh_list_summary(report) == (
        "Обновлено 4 из 10.\n"
        "Маркетплейс заблокировал запрос: 3\n"
        "Не найдено на маркетплейсе: 2\n"
        "Не успели: 1"
    )


def test_list_summary_of_nothing_updated_says_zero_not_success() -> None:
    summary = texts.refresh_list_summary(
        RefreshReport(2, 0, {RefreshFailure.transport_error: 2})
    )

    assert summary == "Обновлено 0 из 2.\nМаркетплейс не ответил: 2"


def test_card_summary_of_a_success_is_one_short_line() -> None:
    summary = texts.refresh_card_summary(RefreshReport(1, 1, {}), Marketplace.wb)

    assert summary == "Цена обновлена."


@pytest.mark.parametrize(
    ("reason", "sentence"),
    [
        (RefreshFailure.transport_error, "Маркетплейс не ответил."),
        (RefreshFailure.out_of_stock, "Товара сейчас нет в наличии."),
        (RefreshFailure.not_found, "Товар на маркетплейсе не нашёлся."),
        (RefreshFailure.bad_payload, "Маркетплейс ответил в неожиданном виде."),
        (RefreshFailure.timed_out, "Не успели получить ответ вовремя."),
        (RefreshFailure.error, "Не удалось сохранить новую цену."),
    ],
)
def test_card_summary_gives_one_reason_and_keeps_the_old_price(
    reason: RefreshFailure, sentence: str
) -> None:
    summary = texts.refresh_card_summary(
        RefreshReport(1, 0, {reason: 1}), Marketplace.wb
    )

    assert summary == f"{sentence} Показана прежняя цена."


@pytest.mark.parametrize(
    ("marketplace", "name"),
    [(Marketplace.ozon, "Ozon"), (Marketplace.wb, "Wildberries")],
)
def test_card_summary_of_a_block_names_the_marketplace(
    marketplace: Marketplace, name: str
) -> None:
    summary = texts.refresh_card_summary(
        RefreshReport(1, 0, {RefreshFailure.blocked: 1}), marketplace
    )

    assert summary == (
        f"{name} заблокировал запрос: он защищается от автоматических обращений. "
        "Показана прежняя цена."
    )


def test_card_summary_of_an_inconsistent_report_does_not_claim_success() -> None:
    summary = texts.refresh_card_summary(RefreshReport(1, 0, {}), Marketplace.wb)

    assert summary == "Не удалось сохранить новую цену. Показана прежняя цена."


def test_progress_and_gone_texts_are_the_agreed_wording() -> None:
    assert texts.REFRESH_IN_PROGRESS_TEXT == "Обновляю цены…"
    assert texts.REFRESH_GONE_TEXT == "Этот товар больше не отслеживается."
    assert texts.REFRESH_LIST_BUTTON_TEXT == "🔄 Обновить цены"
    assert texts.REFRESH_CARD_BUTTON_TEXT == "🔄 Обновить цену"
