"""`SocksProxy` and the state rules of `SocksRelay` (PAB-111 AC2, AC8).

The wire behaviour of the relay and of `check_proxy` is proved on real sockets
in `tests/integration/services/test_socks_relay_live.py`. Expected URLs are
hand-written literals, not rebuilt with the module's own helpers.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.socks_relay import SocksProxy
from app.services.socks_relay import SocksRelay
from app.services.socks_relay import _auth_request


def test_from_url_splits_host_port_and_percent_decodes_the_credentials() -> None:
    proxy = SocksProxy.from_url("socks5://us%40er:p%3Aa%2Fss%25@proxy.example:1080")

    assert str(proxy.host) == "proxy.example"
    assert proxy.port == 1080
    assert str(proxy.username) == "us@er"
    assert str(proxy.password) == "p:a/ss%"


def test_from_url_decodes_multibyte_credentials() -> None:
    proxy = SocksProxy.from_url("socks5://%D0%BB%D0%BE%D0%B3:%D0%BF%D0%B0%D1%80@h:1")

    assert str(proxy.username) == "лог"
    assert str(proxy.password) == "пар"


def test_from_url_without_credentials_leaves_them_none() -> None:
    proxy = SocksProxy.from_url("socks5://proxy.example:1080/")

    assert (proxy.username, proxy.password) == (None, None)
    assert (str(proxy.host), proxy.port) == ("proxy.example", 1080)


def test_from_url_takes_an_ipv6_host_without_brackets() -> None:
    proxy = SocksProxy.from_url("socks5://[2001:db8::1]:1080")

    assert str(proxy.host) == "2001:db8::1"
    assert proxy.port == 1080


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_curl_url_is_socks5h_whatever_scheme_was_configured(scheme: str) -> None:
    proxy = SocksProxy.from_url(f"{scheme}://login:secret@proxy.example:1080")

    assert str(proxy.curl_url) == "socks5h://login:secret@proxy.example:1080"


def test_curl_url_percent_encodes_the_credentials() -> None:
    proxy = SocksProxy.from_url("socks5://us%40er:p%3Aa%2Fss%25@proxy.example:1080")

    assert str(proxy.curl_url) == "socks5h://us%40er:p%3Aa%2Fss%25@proxy.example:1080"


def test_curl_url_encodes_multibyte_credentials() -> None:
    proxy = SocksProxy.from_url("socks5://%D0%BB%D0%BE%D0%B3:%D0%BF%D0%B0%D1%80@h:1")

    assert str(proxy.curl_url) == "socks5h://%D0%BB%D0%BE%D0%B3:%D0%BF%D0%B0%D1%80@h:1"


def test_curl_url_without_credentials_has_no_userinfo() -> None:
    proxy = SocksProxy.from_url("socks5://proxy.example:1080")

    assert str(proxy.curl_url) == "socks5h://proxy.example:1080"


def test_curl_url_puts_an_ipv6_host_in_brackets() -> None:
    proxy = SocksProxy.from_url("socks5://[2001:db8::1]:1080")

    assert str(proxy.curl_url) == "socks5h://[2001:db8::1]:1080"


def test_repr_and_str_hide_host_port_and_credentials() -> None:
    proxy = SocksProxy.from_url(
        "socks5://LOGIN-MARKER:PASS-MARKER@host-marker.example:1081"
    )

    for text in (repr(proxy), str(proxy), f"{proxy}", repr([proxy])):
        assert text in {"SocksProxy(<masked>)", "[SocksProxy(<masked>)]"}


def test_masked_parts_hide_their_value_in_repr_but_keep_it_in_use() -> None:
    proxy = SocksProxy.from_url(
        "socks5://LOGIN-MARKER:PASS-MARKER@host-marker.example:1"
    )

    for part in (proxy.host, proxy.username, proxy.password):
        assert repr(part) == "'***'"
    # positive control: the value is really there for the code that needs it
    assert str(proxy.username) == "LOGIN-MARKER"
    assert proxy.curl_url.startswith("socks5h://LOGIN-MARKER:PASS-MARKER@")


def test_proxy_is_frozen() -> None:
    proxy = SocksProxy.from_url("socks5://proxy.example:1080")

    with pytest.raises(dataclasses.FrozenInstanceError):
        proxy.port = 1  # type: ignore[misc]


def test_relay_repr_hides_everything() -> None:
    relay = SocksRelay(
        SocksProxy.from_url("socks5://LOGIN-MARKER:PASS-MARKER@h-marker:1")
    )

    assert repr(relay) == "SocksRelay(<masked>)"


def test_relay_url_before_start_is_an_error() -> None:
    relay = SocksRelay(SocksProxy.from_url("socks5://proxy.example:1080"))

    with pytest.raises(RuntimeError, match="not started"):
        _ = relay.url


@pytest.mark.asyncio
async def test_exit_of_a_relay_that_never_started_is_a_noop() -> None:
    relay = SocksRelay(SocksProxy.from_url("socks5://proxy.example:1080"))

    await relay.__aexit__(None, None, None)

    with pytest.raises(RuntimeError, match="not started"):
        _ = relay.url


def test_auth_request_is_the_rfc_1929_message_and_hides_itself_in_locals() -> None:
    """The private helper that keeps the message out of `--showlocals`: its bytes
    are the wire format (version 1, length-prefixed login and password), its
    `repr` shows nothing."""
    proxy = SocksProxy.from_url("socks5://login:secret@proxy.example:1080")

    message = _auth_request(proxy)

    assert bytes(message) == b"\x01\x05login\x06secret"
    assert repr(message) == "'***'"
