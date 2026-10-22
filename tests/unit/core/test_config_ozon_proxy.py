"""`OzonSettings.PROXY_URL`: the one place the proxy URL rules live (PAB-111 AC1).

One cell per rule. Every URL carries the markers `LOGINMARK`, `PASSMARK` and
`hostmark`; whatever the failure, neither `str(ValidationError)` nor `repr` /
`model_dump()` of the settings may show them, and the error must name the
field (positive control: the error is really about `PROXY_URL`).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr
from pydantic import ValidationError

from app.core.config import OzonSettings
from app.core.config import Settings
from tests.unit.core.test_config import _delenv_settings_prefix_space
from tests.unit.core.test_config import _make_bot_secret
from tests.unit.core.test_config import _make_db_settings
from tests.unit.core.test_config import _make_redis_settings

_MARKERS = ("LOGINMARK", "PASSMARK", "hostmark")
_BASE = "socks5://LOGINMARK:PASSMARK@hostmark.example:1080"
_LONG_A = "a" * 256
_LOGIN_255 = "LOGINMARK" + "x" * (255 - len("LOGINMARK"))
_LOGIN_256 = "LOGINMARK" + "x" * (256 - len("LOGINMARK"))
_PASS_255 = "PASSMARK" + "y" * (255 - len("PASSMARK"))
_PASS_256 = "PASSMARK" + "y" * (256 - len("PASSMARK"))

_INVALID = {
    "http-scheme": "http://LOGINMARK:PASSMARK@hostmark.example:1080",
    "https-scheme": "https://LOGINMARK:PASSMARK@hostmark.example:1080",
    "socks4-scheme": "socks4://LOGINMARK:PASSMARK@hostmark.example:1080",
    "no-scheme": "LOGINMARK:PASSMARK@hostmark.example:1080",
    "uppercase-scheme-typo": "socks://LOGINMARK:PASSMARK@hostmark.example:1080",
    "port-zero": "socks5://LOGINMARK:PASSMARK@hostmark.example:0",
    "port-65536": "socks5://LOGINMARK:PASSMARK@hostmark.example:65536",
    "port-huge": "socks5://LOGINMARK:PASSMARK@hostmark.example:99999999999",
    "port-not-a-number": "socks5://LOGINMARK:PASSMARK@hostmark.example:abc",
    "port-negative": "socks5://LOGINMARK:PASSMARK@hostmark.example:-1",
    "port-missing": "socks5://LOGINMARK:PASSMARK@hostmark.example",
    "port-empty": "socks5://LOGINMARK:PASSMARK@hostmark.example:",
    "ipv6-port-not-a-number": "socks5://LOGINMARK:PASSMARK@[::1]:abc",
    "host-empty": "socks5://LOGINMARK:PASSMARK@:1080",
    "host-blank": "socks5://LOGINMARK:PASSMARK@ :1080",
    "host-missing-entirely": "socks5://",
    "login-without-password": "socks5://LOGINMARK@hostmark.example:1080",
    "password-without-login": "socks5://:PASSMARK@hostmark.example:1080",
    "empty-password": "socks5://LOGINMARK:@hostmark.example:1080",
    "empty-both": "socks5://:@hostmark.example:1080",
    "path": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080/path",
    "path-slash-deeper": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080//",
    "query": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080?x=1",
    "query-after-slash": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080/?x=1",
    "empty-query": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080?",
    "fragment": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080#frag",
    "empty-fragment": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080#",
    "password-is-invalid-utf8": "socks5://LOGINMARK:%ff@hostmark.example:1080",
    "login-is-invalid-utf8": "socks5://%ffLOGINMARK:PASSMARK@hostmark.example:1080",
    "login-256-bytes": f"socks5://{_LOGIN_256}:PASSMARK@hostmark.example:1080",
    "password-256-bytes": f"socks5://LOGINMARK:{_PASS_256}@hostmark.example:1080",
    "password-256-bytes-multibyte": (
        "socks5://LOGINMARK:" + "%D1%8F" * 128 + "@hostmark.example:1080"
    ),
    "login-256-bytes-percent-decoded": (
        f"socks5://{'%41' * 256}:PASSMARK@hostmark.example:1080"
    ),
}

_VALID = {
    "socks5": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080",
    "socks5h": "socks5h://LOGINMARK:PASSMARK@hostmark.example:1080",
    "root-path": "socks5://LOGINMARK:PASSMARK@hostmark.example:1080/",
    "ipv6": "socks5://LOGINMARK:PASSMARK@[2001:db8::1]:1080",
    "ipv4": "socks5://LOGINMARK:PASSMARK@192.0.2.10:1080",
    "port-1": "socks5://LOGINMARK:PASSMARK@hostmark.example:1",
    "port-65535": "socks5://LOGINMARK:PASSMARK@hostmark.example:65535",
    "percent-encoded-credentials": (
        "socks5://LOGIN%40MARK:PASS%3A%2F%23MARK@hostmark.example:1080"
    ),
    "login-255-bytes": f"socks5://{_LOGIN_255}:PASSMARK@hostmark.example:1080",
    "password-255-bytes": f"socks5://LOGINMARK:{_PASS_255}@hostmark.example:1080",
    "multibyte-credentials-254-bytes": (
        "socks5://" + "%D1%8F" * 127 + ":" + "%D1%8F" * 127 + "@hostmark.example:1080"
    ),
    "no-credentials": "socks5://hostmark.example:1080",
    "no-credentials-socks5h": "socks5h://hostmark.example:1080/",
}


def _settings(**extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        db=_make_db_settings(),
        tg=_make_bot_secret(),
        redis=_make_redis_settings(host="h"),
        **extra,
    )


def _assert_no_markers(text: str) -> None:
    for marker in _MARKERS:
        assert marker not in text, marker
    assert _LONG_A not in text


@pytest.mark.parametrize("url", list(_INVALID.values()), ids=list(_INVALID))
def test_invalid_proxy_url_fails_settings_with_an_error_that_names_the_field_only(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """Loaded the way the application loads it - through `Settings`, whose
    `hide_input_in_errors` keeps the rejected input out of the error."""
    _delenv_settings_prefix_space(monkeypatch)

    with pytest.raises(ValidationError) as excinfo:
        _settings(ozon={"PROXY_URL": url})

    assert [e["loc"] for e in excinfo.value.errors()] == [("ozon", "PROXY_URL")]
    text = str(excinfo.value)
    assert "PROXY_URL" in text  # positive control: the error is about this field
    _assert_no_markers(text)


@pytest.mark.parametrize("url", list(_VALID.values()), ids=list(_VALID))
def test_valid_proxy_url_is_accepted_and_kept_verbatim(url: str) -> None:
    ozon = OzonSettings.model_validate({"PROXY_URL": url})

    assert isinstance(ozon.PROXY_URL, SecretStr)
    assert ozon.PROXY_URL.get_secret_value() == url


@pytest.mark.parametrize("url", ["", " ", "   \t\n"], ids=["empty", "space", "blank"])
def test_empty_or_blank_proxy_url_means_no_proxy(url: str) -> None:
    assert OzonSettings.model_validate({"PROXY_URL": url}).PROXY_URL is None
    assert OzonSettings(PROXY_URL=SecretStr(url)).PROXY_URL is None


def test_proxy_url_is_off_by_default() -> None:
    assert OzonSettings().PROXY_URL is None


def test_surrounding_whitespace_of_a_valid_url_is_stripped() -> None:
    ozon = OzonSettings.model_validate({"PROXY_URL": f"  {_BASE}\n"})

    assert ozon.PROXY_URL is not None
    assert ozon.PROXY_URL.get_secret_value() == _BASE


def test_a_secret_wrapped_url_is_validated_too() -> None:
    assert OzonSettings(PROXY_URL=SecretStr(_BASE)).PROXY_URL is not None
    with pytest.raises(ValidationError):
        OzonSettings(PROXY_URL=SecretStr("http://LOGINMARK:PASSMARK@hostmark:1"))


def test_a_valid_proxy_url_does_not_show_in_repr_str_or_dumps() -> None:
    ozon = OzonSettings.model_validate({"PROXY_URL": _BASE})
    dumps: list[Any] = [
        repr(ozon),
        str(ozon),
        str(ozon.model_dump()),
        ozon.model_dump_json(),
        repr(ozon.PROXY_URL),
    ]

    for text in dumps:
        _assert_no_markers(text)
    # positive control: the value is really held, only hidden
    assert ozon.PROXY_URL is not None
    assert "hostmark" in ozon.PROXY_URL.get_secret_value()


def test_environment_variable_sets_the_proxy_url_and_hides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("OZON__PROXY_URL", _BASE)

    settings = _settings()

    assert settings.ozon.PROXY_URL is not None
    assert settings.ozon.PROXY_URL.get_secret_value() == _BASE
    for text in (repr(settings), str(settings.model_dump())):
        _assert_no_markers(text)


def test_empty_environment_variable_means_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("OZON__PROXY_URL", "")

    assert _settings().ozon.PROXY_URL is None


def test_invalid_environment_variable_fails_settings_without_the_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delenv_settings_prefix_space(monkeypatch)
    monkeypatch.setenv("OZON__PROXY_URL", _INVALID["http-scheme"])

    with pytest.raises(ValidationError) as excinfo:
        _settings()

    assert [e["loc"] for e in excinfo.value.errors()] == [("ozon", "PROXY_URL")]
    assert "PROXY_URL" in str(excinfo.value)
    _assert_no_markers(str(excinfo.value))


def test_env_example_describes_the_proxy_variable_empty_and_without_credentials() -> (
    None
):
    from pathlib import Path

    example = (Path(__file__).resolve().parents[3] / ".env.example").read_text()
    lines = [x for x in example.splitlines() if x.startswith("OZON__PROXY_URL")]

    assert lines == ["OZON__PROXY_URL="]
    assert "socks5h://<login>:<password>@<host>:<port>" in example
