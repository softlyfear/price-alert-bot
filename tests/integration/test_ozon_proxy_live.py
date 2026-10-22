"""`OzonClient` through a SOCKS5 proxy, real `curl_cffi`, no network (PAB-111 AC7 a, c).

The proxy is `SocksStub` (writes down every connection), the "Ozon" behind it
is `OzonAntibotStub`. The names used in URLs do not resolve anywhere: with
`socks5h` the proxy gets the name and the stand-in maps it to the local server,
so a connection that arrives proves the name went to the proxy, not to DNS.
A refused proxy ends as `transport_error`, never `blocked`.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator

import pytest

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceProductData
from app.services.ozon_access import BrowserOzonAccessProvider
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_browser import fetch_ozon_access
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonSession
from app.services.socks_relay import SocksProxy
from tests.integration.browser_probe import processes_started_by_test
from tests.integration.ozon_stub import FIXTURE
from tests.integration.ozon_stub import OzonAntibotStub
from tests.integration.socks_stub import PASSWORD
from tests.integration.socks_stub import USERNAME
from tests.integration.socks_stub import SocksStub
from tests.integration.test_ozon_transport_live import _closed_local_port
from tests.integration.test_ozon_transport_live import _dead_proxy_environment
from tests.unit.ozon_stubs import StubOzonAccess

_FUSE = 30.0
_ARTICLE = 3593896354
_EXPECTED = MarketplaceProductData(
    name="Лонгслив тельняшка длинный рукав 1 шт", price=49500
)
_BODY = f"/body?size={len(FIXTURE.read_bytes()) + 10}"


def _proxy(stub: SocksStub) -> SocksProxy:
    return SocksProxy.from_url(f"socks5://{USERNAME}:{PASSWORD}@127.0.0.1:{stub.port}")


async def _fetch(
    monkeypatch: pytest.MonkeyPatch, url: str, proxy: SocksProxy | None
) -> object:
    monkeypatch.setattr(OzonClient, "URL", url)
    session = OzonSession(proxy)
    try:
        client = OzonClient(session, StubOzonAccess())
        return await asyncio.wait_for(client.get_product_data(_ARTICLE), _FUSE)
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_price_goes_through_the_proxy_with_an_authorised_connect_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        OzonAntibotStub() as ozon,
        SocksStub(targets={"ozon-api.test": ozon_port(ozon)}) as stub,
    ):
        result = await _fetch(
            monkeypatch, f"http://ozon-api.test:80{_BODY}", _proxy(stub)
        )

    assert result == _EXPECTED
    assert stub.connections  # at least one tunnel through the proxy
    authorised = [
        c
        for c in stub.connections
        if (c.username, c.password) == (USERNAME, PASSWORD)
        and (c.atyp, c.host, c.port) == (3, "ozon-api.test", 80)
    ]
    assert authorised


def ozon_port(stub: OzonAntibotStub) -> int:
    return int(stub.base_url.rsplit(":", 1)[1])


@pytest.mark.asyncio
async def test_without_a_proxy_the_proxy_is_never_contacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with OzonAntibotStub() as ozon, SocksStub() as stub:
        result = await _fetch(monkeypatch, ozon.base_url + _BODY, None)

    assert result == _EXPECTED
    assert stub.connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_proxy", [True, False], ids=["proxy", "no-proxy"])
async def test_dead_proxy_environment_variables_change_neither_mode(
    monkeypatch: pytest.MonkeyPatch, with_proxy: bool
) -> None:
    _dead_proxy_environment(monkeypatch)
    with (
        OzonAntibotStub() as ozon,
        SocksStub(targets={"ozon-api.test": ozon_port(ozon)}) as stub,
    ):
        if with_proxy:
            result = await _fetch(
                monkeypatch, f"http://ozon-api.test:80{_BODY}", _proxy(stub)
            )
        else:
            result = await _fetch(monkeypatch, ozon.base_url + _BODY, None)

    assert result == _EXPECTED
    assert len(stub.connections) == (1 if with_proxy else 0)


@pytest.mark.asyncio
async def test_proxy_refusing_the_authorisation_is_transport_error_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with SocksStub(mode="reject_auth") as stub:
        result = await _fetch(
            monkeypatch, f"http://ozon-api.test:80{_BODY}", _proxy(stub)
        )

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail == "ProxyError"


@pytest.mark.asyncio
async def test_unreachable_proxy_is_transport_error_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = SocksProxy.from_url(f"socks5://u:p@127.0.0.1:{_closed_local_port()}")

    result = await _fetch(monkeypatch, f"http://ozon-api.test:80{_BODY}", proxy)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.transport_error
    assert result.detail == "ConnectionError"


# --- (c) the whole chain with a refused proxy ---------------------------------------


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="ozp-", dir="/tmp") as directory:
        monkeypatch.setenv("TMPDIR", directory)
        yield directory


def _provider(proxy: SocksProxy) -> BrowserOzonAccessProvider:
    return BrowserOzonAccessProvider(
        lambda: fetch_ozon_access(
            sandbox=False, home_url="https://shop.example.test/", proxy=proxy
        ),
        token_timeout_seconds=_FUSE,
        failure_pause_seconds=300.0,
        launch_budget=8,
        launch_window_seconds=900.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "detail"),
    [("reject_auth", "proxy_auth_failed"), ("unreachable", "proxy_unreachable")],
)
async def test_refused_proxy_gives_proxy_failed_no_chromium_and_transport_error(
    private_tmp: str, refusal: str, detail: str
) -> None:
    session = OzonSession()
    with SocksStub(mode="reject_auth") as stub:
        proxy = (
            _proxy(stub)
            if refusal == "reject_auth"
            else SocksProxy.from_url(f"socks5://u:p@127.0.0.1:{_closed_local_port()}")
        )
        provider = _provider(proxy)
        try:
            access = await asyncio.wait_for(provider.get(), _FUSE)
            client_result = await asyncio.wait_for(
                OzonClient(session, provider).get_product_data(_ARTICLE), _FUSE
            )
        finally:
            await provider.aclose()
            await session.aclose()

    assert access == OzonAccessUnavailable(OzonAccessStage.proxy_failed, detail)
    assert processes_started_by_test(private_tmp) == {}
    # the second ask is answered by the pause of the provider: still not `blocked`
    assert isinstance(client_result, MarketplaceFetchFailure)
    assert client_result.reason is FetchFailureReason.blocked
    assert client_result.detail == "Ozon access unavailable: paused"
