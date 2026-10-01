"""Tests for app.bot.texts: length limits and disclaimer wiring (AC1, AC3).

The disclaimer wording itself is not re-checked here -- that is
``tests/unit/domain/test_money.py``'s job (the single source of truth,
PAB-067 AC1). This file guards the two properties specific to the bot
layer: the Bot API length limits on the profile fields, and that every
disclaimer-carrying text is built from the imported constant rather than an
independent copy.
"""

from app.bot import texts
from app.domain.money import PRICE_DISCLAIMER_FULL
from app.domain.money import PRICE_DISCLAIMER_SHORT


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
    assert PRICE_DISCLAIMER_FULL in texts.HELP_TEXT


def test_bot_commands_list_is_start_add_help_cancel_in_that_order() -> None:
    assert [c.command for c in texts.BOT_COMMANDS] == [
        "start",
        "add",
        "help",
        "cancel",
    ]


def test_start_and_help_texts_mention_the_add_command() -> None:
    assert "/add" in texts.START_TEXT
    assert "/add" in texts.HELP_TEXT


def test_dialog_texts_with_a_price_carry_the_short_disclaimer_from_money() -> None:
    assert PRICE_DISCLAIMER_SHORT in texts.product_found_text("Item", 199_000)
    assert PRICE_DISCLAIMER_SHORT in texts.confirm_text("Item", 199_000, 150_000)
    assert PRICE_DISCLAIMER_SHORT in texts.added_text("Item", 150_000)


def test_dialog_texts_embed_the_name_verbatim_without_markup() -> None:
    name = "<b>x*"

    assert name in texts.product_found_text(name, 100)
    assert name in texts.confirm_text(name, 100, 50)
    assert name in texts.added_text(name, 50)
