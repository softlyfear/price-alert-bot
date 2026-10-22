"""A stand-in `WbAccessProvider` for tests that need a `WbClient` or the factory.

It is a hand-written fake at the provider boundary (the contract `get` /
`invalidate`), not a copy of `BrowserWbAccessProvider`: it hands out the
current pair, makes a fresh one after `invalidate`, and records every call so
a test can assert on the observable effects.
"""

import functools
from collections.abc import Callable

import httpx

from app.models.enums import Marketplace
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import get_client
from app.services.ozon_client import OzonReply
from app.services.wb_access import WbAccess
from app.services.wb_access import WbAccessProvider
from app.services.wb_access import WbAccessUnavailable
from tests.unit.ozon_stubs import FakeOzonSession
from tests.unit.ozon_stubs import StubOzonAccess


def make_pair(n: int) -> WbAccess:
    """A synthetic pair; the values are recognisable in a request but not real."""
    return WbAccess(token=f"TOKEN-MARKER-{n}", device_id=f"site_{n:032x}")


class StubWbAccess:
    def __init__(self, refusal: WbAccessUnavailable | None = None) -> None:
        self._refusal = refusal
        self._current: WbAccess | None = None
        self._issued = 0
        self.get_calls = 0
        self.invalidated: list[WbAccess] = []
        self.issued: list[WbAccess] = []

    async def get(self) -> WbAccess | WbAccessUnavailable:
        self.get_calls += 1
        if self._refusal is not None:
            return self._refusal
        if self._current is None:
            self._issued += 1
            self._current = make_pair(self._issued)
            self.issued.append(self._current)
        return self._current

    async def invalidate(self, access: WbAccess) -> None:
        self.invalidated.append(access)
        if self._current == access:
            self._current = None


def stub_client_factory(
    access: WbAccessProvider | None = None,
) -> Callable[[Marketplace, httpx.AsyncClient], BaseMarketplaceClient]:
    """The factory shape `main.lifespan` builds: `get_client` bound to a provider."""
    return functools.partial(
        get_client,
        wb_access=access or StubWbAccess(),
        ozon_access=StubOzonAccess(),
        ozon_session=FakeOzonSession(OzonReply(200, b"")),
    )
