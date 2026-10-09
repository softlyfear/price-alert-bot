"""Guard: every callback popup text fits the Bot API limit (PAB-074 AC).

``answerCallbackQuery.text`` is limited to 200 characters, counted by Telegram
in UTF-16 code units. A longer text is a ``TelegramBadRequest`` and a button
that gets no answer. The list below is explicit and was checked against
``show_alert=True`` call sites in ``app/handlers`` plus the paths that reach a
popup through ``reply_to_callback`` when ``callback.message`` is ``None``.
"""

import pytest

from app.bot import texts

_LIMIT = 200

_POPUP_TEXTS: dict[str, str] = {
    "ADD_STALE_ALERT_TEXT": texts.ADD_STALE_ALERT_TEXT,
    "ADD_OLD_CARD_ALERT_TEXT": texts.ADD_OLD_CARD_ALERT_TEXT,
    "LIST_GONE_ALERT_TEXT": texts.LIST_GONE_ALERT_TEXT,
    "LIST_STALE_BUTTON_ALERT_TEXT": texts.LIST_STALE_BUTTON_ALERT_TEXT,
    "LIST_ALERT_REMOVED_TEXT": texts.LIST_ALERT_REMOVED_TEXT,
    "LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT": texts.LIST_ALERT_REMOVED_WITH_PRODUCT_TEXT,
    "LIST_PRODUCT_REMOVED_TEXT": texts.LIST_PRODUCT_REMOVED_TEXT,
    "CANCEL_TEXT": texts.CANCEL_TEXT,
}


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fits(text: str) -> bool:
    return _utf16_len(text) <= _LIMIT


def test_popup_list_has_the_eight_known_constants() -> None:
    assert len(_POPUP_TEXTS) == 8


@pytest.mark.parametrize("name", list(_POPUP_TEXTS))
def test_popup_text_fits_the_bot_api_limit(name: str) -> None:
    text = _POPUP_TEXTS[name]

    assert text, name
    assert _fits(text), f"{name}: {_utf16_len(text)} UTF-16 units"


def test_predicate_accepts_exactly_200_units() -> None:
    assert _fits("a" * 200)


def test_predicate_rejects_201_units() -> None:
    assert not _fits("a" * 201)


def test_predicate_counts_utf16_units_not_code_points() -> None:
    text = "a" * 199 + "\U0001f600"  # 200 code points, 201 UTF-16 units

    assert len(text) == 200
    assert not _fits(text)
