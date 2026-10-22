"""The "price when added" line of the card screen (PAB-104 AC4).

The clock of the screen is frozen to 2026-10-21 12:00 UTC by the autouse
fixture of ``conftest.py``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.handlers.product_card import card_screen
from tests.unit.handlers.card_dialog_support import NB
from tests.unit.handlers.card_dialog_support import card

MOSCOW = ZoneInfo("Europe/Moscow")


def _screen_text(
    initial_price: int | None,
    created_at: datetime,
    tz: ZoneInfo = MOSCOW,
) -> str:
    tracked = card()
    tracked.product.initial_price = initial_price
    tracked.product.created_at = created_at
    return card_screen(tracked, tz)[0]


def test_card_initial_line_has_exact_text_for_a_known_change() -> None:
    # 1 755 rubles now vs 1 500 at adding: +255 rubles, +17,0 %
    text = _screen_text(150_000, datetime(2025, 10, 14, 22, 30, tzinfo=UTC))

    assert (
        f"Текущая цена: 1{NB}755{NB}₽ (цена на 14:05)\n"
        f"При добавлении: 1{NB}500{NB}₽ (15.10.2025) · +255{NB}₽ (+17,0{NB}%)\n"
        "Цена без учёта персональных скидок.\n"
    ) in text


def test_card_date_uses_the_display_zone_passed_to_the_screen() -> None:
    created = datetime(2026, 10, 14, 22, 30, tzinfo=UTC)

    assert "(15.10)" in _screen_text(150_000, created, MOSCOW)
    assert "(14.10)" in _screen_text(150_000, created, ZoneInfo("UTC"))


def test_card_without_initial_price_has_no_initial_line() -> None:
    text = _screen_text(None, datetime(2026, 10, 14, 22, 30, tzinfo=UTC))

    assert "При добавлении" not in text
    assert f"Текущая цена: 1{NB}755{NB}₽" in text


def test_card_disclaimer_appears_exactly_once_with_the_initial_line() -> None:
    text = _screen_text(150_000, datetime(2026, 10, 14, 22, 30, tzinfo=UTC))

    assert text.count(PRICE_DISCLAIMER_SHORT) == 1


def test_card_shows_the_line_for_a_zero_initial_price() -> None:
    text = _screen_text(0, datetime(2026, 10, 14, 22, 30, tzinfo=UTC))

    assert (
        f"Текущая цена: 1{NB}755{NB}₽ (цена на 14:05)\n"
        f"При добавлении: 0{NB}₽ (15.10) · +1{NB}755{NB}₽\n"
    ) in text
