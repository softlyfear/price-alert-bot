"""Wildberries size is part of the public link identity."""

import pytest

from app.domain.exceptions import MultipleProductRefsError
from app.domain.exceptions import ProductRefParseError
from app.domain.links import MAX_ARTICLE
from app.domain.links import ProductRef
from app.domain.links import build_product_url
from app.domain.links import extract_product_ref
from app.domain.links import parse_product_ref
from app.models.enums import Marketplace

_URL = "https://www.wildberries.ru/catalog/123/detail.aspx"


@pytest.mark.parametrize(
    "query",
    [
        "size=",
        "size=0",
        "size=-1",
        "size=１２",
        "size=9223372036854775808",
        "size=1&size=2",
    ],
)
def test_invalid_size_is_never_silently_legacy(query: str) -> None:
    with pytest.raises(ProductRefParseError):
        parse_product_ref(f"{_URL}?{query}")


def test_selected_link_round_trips_at_maximum() -> None:
    url = build_product_url(Marketplace.wb, 123, wb_size_id=MAX_ARTICLE)
    assert url == f"{_URL}?size=9223372036854775807"
    assert parse_product_ref(url) == ProductRef(Marketplace.wb, 123, MAX_ARTICLE)


def test_different_sizes_are_distinct_in_shared_text() -> None:
    with pytest.raises(MultipleProductRefsError):
        extract_product_ref(f"{_URL}?size=1 {_URL}?size=2")
