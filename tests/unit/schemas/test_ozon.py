"""Unit tests for app.schemas.ozon (PAB-071).

The reference facts come from `tests/fixtures/ozon/entrypoint_3593896354.json`,
a minimal slice of the customer-supplied `entrypoint-api` response for article
`3593896354` (`PROJECT.md` sections 2.11 and 8.2). Compared with the source it
removes, with reasons:

- every top-level key except `widgetStates` - they hold personal or session
  data (`browser.ip`, `location`, `userToken`, `pageToken`, `requestID`,
  `trackingTokenAliases`, the `sh` parameter of `pageInfo.url`);
- 77 of the 81 `widgetStates` entries - only the three read widgets and the
  `webPriceDecreasedCompact` prefix-trap witness remain;
- three opaque token keys at any depth inside the kept states:
  `trackingInfo`, `trackingInfoOzonBank`, `cellTrackingInfo`.

Everything else, including U+2009 and U+20BD in the price strings, is
verbatim. Every cell built inline in this module (price strings, limit
payloads, the extra key) is synthetic and marked as such; none is a claim
about the Ozon contract beyond section 2.11. No network, no asyncio.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.ozon import OzonDetailSkuState
from app.schemas.ozon import OzonEntrypointResponse
from app.schemas.ozon import OzonPriceState
from app.schemas.ozon import OzonProductHeadingState

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "ozon"
    / "entrypoint_3593896354.json"
)

# U+2009 THIN SPACE group/sign separator, U+20BD ruble sign, U+00A0 NBSP:
# spelled out so the look-alike characters stay visible in review.
_T = "\u2009"
_NBSP = "\u00a0"
_RUB = "\u20bd"


@pytest.fixture(scope="module")
def states() -> dict[str, str]:
    """`widgetStates` of the real fixture, parsed through the schema."""
    parsed = OzonEntrypointResponse.model_validate_json(_FIXTURE_PATH.read_bytes())
    return parsed.widget_states


def _state(states: dict[str, str], component: str) -> str:
    matches = [v for k, v in states.items() if k.partition("-")[0] == component]
    assert len(matches) == 1
    return matches[0]


def _price(text: Any) -> OzonPriceState:
    return OzonPriceState.model_validate({"isAvailable": True, "price": text})


# --- the real fixture ---------------------------------------------------------


def test_fixture_top_level_has_widget_states_with_four_keys(
    states: dict[str, str],
) -> None:
    assert sorted(k.partition("-")[0] for k in states) == [
        "webDetailSKU",
        "webPrice",
        "webPriceDecreasedCompact",
        "webProductHeading",
    ]


def test_fixture_heading_title_is_37_characters_verbatim(
    states: dict[str, str],
) -> None:
    heading = OzonProductHeadingState.model_validate_json(
        _state(states, "webProductHeading")
    )

    assert heading.title == "Лонгслив тельняшка длинный рукав 1 шт"
    assert len(heading.title) == 37


def test_fixture_price_is_49500_kopecks_without_the_card_price(
    states: dict[str, str],
) -> None:
    """Page shows 495 (no Ozon card), 471 (with card), 1 742 (crossed out):
    the tracked value is the first, `PROJECT.md` section 2.11 item 5."""
    price = OzonPriceState.model_validate_json(_state(states, "webPrice"))

    assert price.price == 49500
    assert price.is_available is True


def test_fixture_copy_text_is_the_requested_article(states: dict[str, str]) -> None:
    sku = OzonDetailSkuState.model_validate_json(_state(states, "webDetailSKU"))

    assert sku.copy_text == "3593896354"


# --- price conversion matrix (synthetic cells) --------------------------------


@pytest.mark.parametrize(
    ("text", "kopecks"),
    [
        (f"495{_T}{_RUB}", 49500),
        (f"1{_T}742{_T}{_RUB}", 174200),
        (f"1{_T}000{_T}000{_T}{_RUB}", 100000000),
        (f"1{_T}{_RUB}", 100),
        (f"999{_T}{_RUB}", 99900),
        (f"10{_T}000{_T}{_RUB}", 1000000),
    ],
    ids=["495", "1742", "million", "one-ruble", "999", "10000"],
)
def test_price_accepted_forms_convert_to_kopecks(text: str, kopecks: int) -> None:
    """Synthetic cells; the first is the observed form."""
    assert _price(text).price == kopecks


@pytest.mark.parametrize(
    "text",
    [
        f"1{_NBSP}742{_T}{_RUB}",
        f"1 742{_T}{_RUB}",
        f"1{_T}742{_NBSP}{_RUB}",
        f"1{_T}742 {_RUB}",
        f"495{_NBSP}{_RUB}",
        f"495 {_RUB}",
        f"1742{_T}{_RUB}",
        f"495,50{_T}{_RUB}",
        f"495.50{_T}{_RUB}",
        "495",
        _RUB,
        "",
        f"-495{_T}{_RUB}",
        f"0495{_T}{_RUB}",
        f"0{_T}{_RUB}",
        f"1{_T}74{_T}{_RUB}",
        f"1{_T}7424{_T}{_RUB}",
        f"1{_T}742{_RUB}",
        f"495{_T}$",
        f"495{_T}€",
        f" 495{_T}{_RUB}",
        f"{_T}495{_T}{_RUB}",
        f"495{_T}{_RUB} ",
        f"495{_T}{_RUB}{_T}",
        f"495{_T}{_RUB}\n",
        f"\n495{_T}{_RUB}",
        f"٤٩٥{_T}{_RUB}",
        f"４９５{_T}{_RUB}",
        f"1{_T}٧٤٢{_T}{_RUB}",
    ],
    ids=[
        "nbsp-group",
        "space-group",
        "nbsp-before-sign",
        "space-before-sign",
        "nbsp-only",
        "space-only",
        "no-group-separator",
        "comma-fraction",
        "dot-fraction",
        "no-sign",
        "sign-only",
        "empty",
        "negative",
        "leading-zero",
        "zero",
        "short-group",
        "long-group",
        "no-space-before-sign",
        "dollar",
        "euro",
        "leading-space",
        "leading-thin-space",
        "trailing-space",
        "trailing-thin-space",
        "trailing-newline",
        "leading-newline",
        "arabic-indic-digits",
        "fullwidth-digits",
        "arabic-indic-in-group",
    ],
)
def test_price_rejected_forms_raise_validation_error(text: str) -> None:
    """Synthetic cells. Non-ASCII digits are accepted by `str.isdigit` and
    `int`, so a lazy implementation passes them; the schema must not."""
    with pytest.raises(ValidationError):
        _price(text)


@pytest.mark.parametrize(
    "value",
    [495, 495.0, None, True, ["495"], b"495"],
    ids=["int", "float", "none", "bool", "list", "bytes"],
)
def test_price_non_string_is_rejected(value: Any) -> None:
    with pytest.raises(ValidationError):
        _price(value)


def test_price_of_exactly_32_characters_is_accepted() -> None:
    """Synthetic boundary of the pre-regex length guard: 2 + 7*4 + 2 = 32."""
    text = f"10{_T}000" * 1 + f"{_T}000" * 6 + f"{_T}{_RUB}"
    assert len(text) == 32

    assert _price(text).price == int("10" + "000" * 7) * 100


def test_price_of_33_characters_is_rejected_though_well_formed() -> None:
    """Synthetic: matches the format, only the length guard rejects it."""
    text = f"100{_T}000" + f"{_T}000" * 6 + f"{_T}{_RUB}"
    assert len(text) == 33

    with pytest.raises(ValidationError):
        _price(text)


def test_price_huge_string_is_rejected_fast() -> None:
    with pytest.raises(ValidationError):
        _price("9" * 1_000_000)


# --- strict isAvailable -------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["true", "false", 1, 0, None],
    ids=["true-str", "false-str", "1", "0", "null"],
)
def test_is_available_must_be_a_real_bool(value: Any) -> None:
    with pytest.raises(ValidationError):
        OzonPriceState.model_validate({"isAvailable": value, "price": f"1{_T}{_RUB}"})


def test_is_available_false_is_a_valid_value() -> None:
    state = OzonPriceState.model_validate(
        {"isAvailable": False, "price": f"1{_T}{_RUB}"}
    )

    assert state.is_available is False


def test_price_state_requires_both_fields() -> None:
    with pytest.raises(ValidationError):
        OzonPriceState.model_validate({"price": f"1{_T}{_RUB}"})
    with pytest.raises(ValidationError):
        OzonPriceState.model_validate({"isAvailable": True})


# --- extra="ignore" and frozen ------------------------------------------------


def test_unknown_top_level_key_is_ignored() -> None:
    """Synthetic extra key next to `widgetStates`."""
    parsed = OzonEntrypointResponse.model_validate(
        {"widgetStates": {"a-1": "{}"}, "synthetic_extra": {"x": 1}}
    )

    assert parsed.widget_states == {"a-1": "{}"}


def test_unknown_widget_state_keys_are_ignored() -> None:
    """Synthetic extra keys inside each read state."""
    heading = OzonProductHeadingState.model_validate(
        {"title": "T", "synthetic_extra": 1}
    )
    sku = OzonDetailSkuState.model_validate({"copyText": "1", "synthetic_extra": 1})
    price = OzonPriceState.model_validate(
        {
            "isAvailable": True,
            "price": f"1{_T}{_RUB}",
            "cardPrice": f"1{_T}{_RUB}",
            "synthetic_extra": 1,
        }
    )

    assert (heading.title, sku.copy_text, price.price) == ("T", "1", 100)


def test_models_are_frozen() -> None:
    heading = OzonProductHeadingState.model_validate({"title": "T"})

    with pytest.raises(ValidationError):
        heading.title = "other"  # type: ignore[misc]


# --- limits and strictness of the envelope (synthetic payloads) ---------------


def test_widget_states_with_2000_entries_is_accepted() -> None:
    body = {"widgetStates": {f"w{i}-1": "{}" for i in range(2000)}}

    assert len(OzonEntrypointResponse.model_validate(body).widget_states) == 2000


def test_widget_states_with_2001_entries_is_rejected() -> None:
    body = {"widgetStates": {f"w{i}-1": "{}" for i in range(2001)}}

    with pytest.raises(ValidationError):
        OzonEntrypointResponse.model_validate(body)


def test_widget_key_of_256_characters_is_accepted_and_257_rejected() -> None:
    assert OzonEntrypointResponse.model_validate({"widgetStates": {"k" * 256: "{}"}})
    with pytest.raises(ValidationError):
        OzonEntrypointResponse.model_validate({"widgetStates": {"k" * 257: "{}"}})


def test_widget_state_of_4_mebibytes_is_accepted_and_one_more_rejected() -> None:
    limit = 4 * 1024 * 1024

    assert OzonEntrypointResponse.model_validate({"widgetStates": {"a-1": "x" * limit}})
    with pytest.raises(ValidationError):
        OzonEntrypointResponse.model_validate(
            {"widgetStates": {"a-1": "x" * (limit + 1)}}
        )


@pytest.mark.parametrize(
    "value",
    [5, None, {"a": 1}, ["x"], True],
    ids=["int", "null", "dict", "list", "bool"],
)
def test_widget_state_value_must_be_a_string(value: Any) -> None:
    with pytest.raises(ValidationError):
        OzonEntrypointResponse.model_validate({"widgetStates": {"a-1": value}})


@pytest.mark.parametrize(
    "body",
    [{}, {"widgetStates": None}, {"widgetStates": []}, []],
    ids=["missing", "null", "list", "top-level-list"],
)
def test_envelope_without_widget_states_object_is_rejected(body: Any) -> None:
    with pytest.raises(ValidationError):
        OzonEntrypointResponse.model_validate(body)


def test_title_of_1000_characters_is_accepted_and_1001_rejected() -> None:
    assert OzonProductHeadingState.model_validate({"title": "t" * 1000})
    with pytest.raises(ValidationError):
        OzonProductHeadingState.model_validate({"title": "t" * 1001})


def test_copy_text_of_64_characters_is_accepted_and_65_rejected() -> None:
    assert OzonDetailSkuState.model_validate({"copyText": "1" * 64})
    with pytest.raises(ValidationError):
        OzonDetailSkuState.model_validate({"copyText": "1" * 65})


@pytest.mark.parametrize(
    "value", [3593896354, None, ["1"]], ids=["int", "null", "list"]
)
def test_title_and_copy_text_must_be_strings(value: Any) -> None:
    with pytest.raises(ValidationError):
        OzonProductHeadingState.model_validate({"title": value})
    with pytest.raises(ValidationError):
        OzonDetailSkuState.model_validate({"copyText": value})


def test_fixture_states_round_trip_as_json_strings(states: dict[str, str]) -> None:
    """Sanity: every value is a JSON string (double encoding is the fact)."""
    for value in states.values():
        assert isinstance(json.loads(value), dict)
