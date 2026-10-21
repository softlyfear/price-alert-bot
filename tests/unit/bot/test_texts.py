"""Tests for app.bot.texts: length limits and disclaimer wiring (AC1, AC3).

The disclaimer wording itself is not re-checked here -- that is
``tests/unit/domain/test_money.py``'s job (the single source of truth,
PAB-067 AC1). This file guards the two properties specific to the bot
layer: the Bot API length limits on the profile fields, and that every
disclaimer-carrying text is built from the imported constant rather than an
independent copy.
"""

from typing import cast

import pytest

from app.bot import texts
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT
from app.models.enums import AlertDirection


def test_bot_description_contains_full_disclaimer_and_fits_bot_api_limit() -> None:
    """Bot API's ``setMyDescription`` caps ``description`` at 512 characters.

    Mutation target: replacing ``+ PRICE_DISCLAIMER_FULL`` with a literal
    copy of the same text with one character changed.
    """
    assert PRICE_DISCLAIMER_FULL in texts.BOT_DESCRIPTION
    assert len(texts.BOT_DESCRIPTION) <= 512


def test_bot_short_description_contains_short_disclaimer_and_fits_bot_api_limit() -> (
    None
):
    """Bot API's ``setMyShortDescription`` caps ``short_description`` at 120
    characters -- the ticket measured the *full* disclaimer alone (137
    chars) does not fit, so the short description must use the short
    wording, not the full one.

    Mutation target: substituting ``PRICE_DISCLAIMER_FULL`` for
    ``PRICE_DISCLAIMER_SHORT`` in ``BOT_SHORT_DESCRIPTION``.
    """
    assert PRICE_DISCLAIMER_SHORT in texts.BOT_SHORT_DESCRIPTION
    assert PRICE_DISCLAIMER_FULL not in texts.BOT_SHORT_DESCRIPTION
    assert len(texts.BOT_SHORT_DESCRIPTION) <= 120


def test_start_text_contains_full_disclaimer() -> None:
    assert PRICE_DISCLAIMER_FULL in texts.START_TEXT


def test_help_text_contains_full_disclaimer() -> None:
    assert PRICE_DISCLAIMER_FULL in texts.help_text(50)


def test_bot_commands_list_is_start_help_cancel_in_that_order() -> None:
    assert [c.command for c in texts.BOT_COMMANDS] == ["start", "help", "cancel"]


def test_start_text_lists_no_commands() -> None:
    assert "/add" not in texts.START_TEXT
    assert "/list" not in texts.START_TEXT


@pytest.mark.parametrize("command", ["/start", "/add", "/list", "/help", "/cancel"])
def test_help_text_lists_every_command(command: str) -> None:
    assert command in texts.help_text(50)


@pytest.mark.parametrize("limit", [7, 50])
def test_help_text_names_the_given_product_limit(limit: int) -> None:
    other = 50 if limit == 7 else 7

    assert f"Лимит товаров: {limit}." in texts.help_text(limit)
    assert f"Лимит товаров: {other}." not in texts.help_text(limit)


@pytest.mark.parametrize("n", [1, 2, 5, 21, 50])
def test_help_text_limit_phrase_is_the_same_for_every_number(n: int) -> None:
    lines = texts.help_text(n).split("\n")

    assert f"Лимит товаров: {n}." in lines


@pytest.mark.parametrize("n", [1, 2, 5, 21, 50])
def test_limit_exceeded_text_is_the_exact_phrase_for_every_number(n: int) -> None:
    assert texts.limit_exceeded_text(n) == (
        f"Достигнут лимит товаров: {n}. "
        "Чтобы добавить новый, сначала перестаньте следить за одним из текущих."
    )


@pytest.mark.parametrize("limit", [1, 50, 1000])
def test_help_text_fits_the_telegram_message_limit(limit: int) -> None:
    assert len(texts.help_text(limit)) <= 4096


def test_start_help_and_added_texts_carry_the_one_sound_reminder() -> None:
    assert texts.SOUND_REMINDER_TEXT in texts.START_TEXT
    assert texts.SOUND_REMINDER_TEXT in texts.help_text(50)
    assert texts.SOUND_REMINDER_TEXT in texts.added_text(
        "Item", 150_000, AlertDirection.below
    )


def test_help_text_does_not_name_a_cooldown_number_or_promise_editing() -> None:
    text = texts.help_text(50)

    assert "изменить" not in text.lower()
    assert "минут" not in text


_NBSP = "\u00a0"


@pytest.mark.parametrize("direction", list(AlertDirection))
def test_dialog_texts_with_a_price_carry_the_short_disclaimer_from_money(
    direction: AlertDirection,
) -> None:
    assert PRICE_DISCLAIMER_SHORT in texts.product_found_text("Item", 199_000)
    assert PRICE_DISCLAIMER_SHORT in texts.confirm_text(
        "Item", 199_000, 150_000, direction
    )
    assert PRICE_DISCLAIMER_SHORT in texts.added_text("Item", 150_000, direction)


def test_dialog_texts_embed_the_name_verbatim_without_markup() -> None:
    name = "<b>x*"
    below = AlertDirection.below

    assert name in texts.product_found_text(name, 100)
    assert name in texts.confirm_text(name, 100, 50, below)
    assert name in texts.added_text(name, 50, below)


def test_direction_phrase_names_each_direction_with_the_formatted_price() -> None:
    assert (
        texts.direction_phrase(AlertDirection.below, 150_000)
        == f"цена станет ниже 1{_NBSP}500{_NBSP}₽"
    )
    assert (
        texts.direction_phrase(AlertDirection.above, 250_000)
        == f"цена станет выше 2{_NBSP}500{_NBSP}₽"
    )


def test_direction_phrase_rejects_a_value_outside_the_enum() -> None:
    with pytest.raises(AssertionError):
        texts.direction_phrase(cast(AlertDirection, "sideways"), 100)


def test_confirm_and_success_texts_use_the_phrase_of_their_direction() -> None:
    above_confirm = texts.confirm_text("Item", 199_000, 250_000, AlertDirection.above)
    below_confirm = texts.confirm_text("Item", 199_000, 150_000, AlertDirection.below)
    above_added = texts.added_text("Item", 250_000, AlertDirection.above)
    below_added = texts.added_text("Item", 150_000, AlertDirection.below)

    assert f"Сообщу, когда цена станет выше 2{_NBSP}500{_NBSP}₽." in above_confirm
    assert f"Сообщу, когда цена станет ниже 1{_NBSP}500{_NBSP}₽." in below_confirm
    assert f"Сообщу, когда цена станет выше 2{_NBSP}500{_NBSP}₽." in above_added
    assert f"Сообщу, когда цена станет ниже 1{_NBSP}500{_NBSP}₽." in below_added
    assert "ниже 2" not in above_confirm
    assert "выше 1" not in below_confirm


def test_price_prompt_explains_both_outcomes() -> None:
    assert "снизится" in texts.ADD_PROMPT_PRICE_TEXT
    assert "вырастет" in texts.ADD_PROMPT_PRICE_TEXT


@pytest.mark.parametrize(
    "text",
    [
        texts.START_TEXT,
        texts.help_text(50),
        texts.BOT_DESCRIPTION,
        texts.BOT_SHORT_DESCRIPTION,
    ],
    ids=["start", "help", "description", "short-description"],
)
def test_profile_and_intro_texts_mention_both_directions(text: str) -> None:
    lowered = text.lower()
    assert "сниж" in lowered or "снизи" in lowered
    assert "рост" in lowered or "растёт" in lowered or "вырастет" in lowered


def test_unexpected_error_text_says_the_action_may_not_have_happened() -> None:
    assert "могло не выполниться" in texts.UNEXPECTED_ERROR_TEXT
    assert "ничего не сохранено" not in texts.UNEXPECTED_ERROR_TEXT.lower()


def test_checking_text_is_short_plain_and_has_no_price() -> None:
    """The notice is edited into the outcome and carries no price (PAB-090 AC6)."""
    text = texts.ADD_CHECKING_TEXT

    assert text.strip() != ""
    assert len(text) <= 200
    assert not any(ch.isdigit() for ch in text)
    assert "₽" not in text
    assert "<" not in text
    assert ">" not in text
