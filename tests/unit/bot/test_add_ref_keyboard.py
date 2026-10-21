"""Tests for ``AddRefCb`` and ``add_ref_keyboard`` (PAB-094)."""

import pytest
from pydantic import ValidationError

from app.bot.keyboards import AddRefCb
from app.bot.keyboards import add_ref_keyboard
from app.domain.links import MAX_ARTICLE
from app.models.enums import Marketplace

_BOT_API_CALLBACK_LIMIT = 64


@pytest.mark.parametrize("marketplace", list(Marketplace))
def test_packed_payload_with_the_largest_article_fits_the_bot_api_limit(
    marketplace: Marketplace,
) -> None:
    packed = AddRefCb(marketplace=marketplace, article=MAX_ARTICLE).pack()

    assert len(packed.encode()) <= _BOT_API_CALLBACK_LIMIT


@pytest.mark.parametrize("marketplace", list(Marketplace))
@pytest.mark.parametrize("article", [1, 12345678, MAX_ARTICLE])
def test_pack_and_unpack_give_the_same_values(
    marketplace: Marketplace, article: int
) -> None:
    restored = AddRefCb.unpack(
        AddRefCb(marketplace=marketplace, article=article).pack()
    )

    assert (restored.marketplace, restored.article) == (marketplace, article)


def test_packed_payload_format_is_prefix_marketplace_article() -> None:
    assert AddRefCb(marketplace=Marketplace.ozon, article=42).pack() == "addref:ozon:42"


@pytest.mark.parametrize("article", [0, -1, MAX_ARTICLE + 1])
def test_article_outside_the_valid_range_is_rejected(article: int) -> None:
    with pytest.raises(ValidationError):
        AddRefCb(marketplace=Marketplace.wb, article=article)


def test_keyboard_has_the_add_button_then_the_main_menu_button() -> None:
    markup = add_ref_keyboard(Marketplace.wb, 12345678)

    assert [
        [(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard
    ] == [
        [("➕ Добавить этот товар", "addref:wb:12345678")],
        [("Главное меню", "menu:start")],
    ]
