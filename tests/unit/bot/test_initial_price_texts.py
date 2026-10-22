"""Texts of the "price when added" card line (PAB-104 AC1-AC3).

Expected strings are written out by hand: ``\\u00a0`` is a no-break space,
``\\u2212`` is the minus sign.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.bot import texts
from app.models.enums import AlertDirection
from app.models.enums import Marketplace

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 22, 11, 5, tzinfo=UTC)


def test_date_without_year_when_added_in_the_current_year() -> None:
    created = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    assert texts.added_date_label(created, NOW, MOSCOW) == "15.10"


def test_date_with_year_when_added_in_another_year() -> None:
    created = datetime(2025, 10, 15, 12, 0, tzinfo=UTC)

    assert texts.added_date_label(created, NOW, MOSCOW) == "15.10.2025"


def test_date_is_taken_in_the_display_zone_not_in_utc() -> None:
    created = datetime(2026, 10, 14, 22, 30, tzinfo=UTC)  # 01:30 on the 15th, MSK

    assert texts.added_date_label(created, NOW, MOSCOW) == "15.10"


def test_year_is_compared_in_the_display_zone_for_the_added_date() -> None:
    # 2025-12-31 22:00 UTC is already 2026-01-01 01:00 in Moscow: current year.
    created = datetime(2025, 12, 31, 22, 0, tzinfo=UTC)

    assert texts.added_date_label(created, NOW, MOSCOW) == "01.01"


def test_year_is_compared_in_the_display_zone_for_now() -> None:
    # now is 2026-12-31 22:00 UTC = 2027-01-01 in Moscow, so 2026 is "another year".
    now = datetime(2026, 12, 31, 22, 0, tzinfo=UTC)
    created = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    assert texts.added_date_label(created, now, MOSCOW) == "15.10.2026"


_N = "\u00a0"  # no-break space: thousands separator, before "₽" and "%"
_M = "\u2212"  # minus sign
_HEAD = f"При добавлении: 1{_N}000{_N}₽ (15.10) · "
_HEAD10 = f"При добавлении: 10{_N}000{_N}₽ (15.10) · "


@pytest.mark.parametrize(
    ("initial", "current", "expected"),
    [
        (
            199_000,
            179_900,
            f"При добавлении: 1{_N}990{_N}₽ (15.10) · {_M}191{_N}₽ ({_M}9,6{_N}%)",
        ),
        (100_000, 112_345, f"{_HEAD}+123,45{_N}₽ (+12,3{_N}%)"),
        (100_000, 100_250, f"{_HEAD}+2,50{_N}₽ (+0,3{_N}%)"),
        (100_000, 0, f"{_HEAD}{_M}1{_N}000{_N}₽ ({_M}100,0{_N}%)"),
        (1_000_000, 1_000_050, f"{_HEAD10}+0,50{_N}₽ (менее 0,1{_N}%)"),
        (100_000, 100_000, f"{_HEAD}без изменений"),
        (0, 5_000, f"При добавлении: 0{_N}₽ (15.10) · +50{_N}₽"),
        (100_000, 110_000, f"{_HEAD}+100{_N}₽ (+10,0{_N}%)"),
        (100_000, 350_000, f"{_HEAD}+2{_N}500{_N}₽ (+250,0{_N}%)"),
        (100_000, 99_750, f"{_HEAD}{_M}2,50{_N}₽ ({_M}0,3{_N}%)"),
        (1_000_000, 999_950, f"{_HEAD10}{_M}0,50{_N}₽ (менее 0,1{_N}%)"),
    ],
)
def test_initial_price_line_is_exact(initial: int, current: int, expected: str) -> None:
    assert texts.initial_price_text(initial, current, "15.10") == expected


_URL = "https://www.wildberries.ru/catalog/1/detail.aspx"
_ALERTS = [(AlertDirection.below, 150_000)]
_NB = " "


def test_card_text_puts_the_initial_line_right_after_the_current_price() -> None:
    line = f"При добавлении: 2{_NB}000{_NB}₽ (15.10) · без изменений"

    text = texts.product_card_text(
        "Item", Marketplace.wb, _URL, 200_000, _ALERTS, initial_line=line
    )

    assert text == (
        "Item\n"
        "Маркетплейс: Wildberries\n"
        f"{_URL}\n"
        f"Текущая цена: 2{_NB}000{_NB}₽\n"
        f"{line}\n"
        "Цена без учёта персональных скидок.\n"
        "\n"
        "Пороги:\n"
        f"📉 1{_NB}500{_NB}₽"
    )


def test_card_text_is_unchanged_without_the_initial_line() -> None:
    text = texts.product_card_text(
        "Item", Marketplace.wb, _URL, 200_000, _ALERTS, initial_line=None
    )

    assert text == (
        "Item\n"
        "Маркетплейс: Wildberries\n"
        f"{_URL}\n"
        f"Текущая цена: 2{_NB}000{_NB}₽\n"
        "Цена без учёта персональных скидок.\n"
        "\n"
        "Пороги:\n"
        f"📉 1{_NB}500{_NB}₽"
    )
