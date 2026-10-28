"""Real Chromium and real `curl_cffi` against a local anti-bot stand-in (PAB-110 AC11).

`fetch_ozon_access` drives a real Chromium (`sandbox=False`: the sandbox does
not start on the development host) against `OzonAntibotStub` on `127.0.0.1`;
`OzonSession` is the real `curl_cffi` transport. Nothing here talks to Ozon,
and a pass proves only the protocol of `PROJECT.md` 2.14 O12, not the real
check. A missing Chromium is a red test, not a skip.

`cookie_domain="127.0.0.1"` stands in for `ozon.ru`: the stand-in has no such
name. The domain filter itself is proved in the unit tests.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from app.schemas.marketplace import MarketplaceProductData
from app.services.ozon_access import BrowserOzonAccessProvider
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_browser import fetch_ozon_access
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonSession
from tests.integration.browser_probe import observe_chromium_environments
from tests.integration.browser_probe import processes_started_by_test
from tests.integration.ozon_stub import OzonAntibotStub

if TYPE_CHECKING:
    pass

_FUSE = 60.0
_ARTICLE = 3593896354
_EXPECTED = MarketplaceProductData(
    name="Лонгслив тельняшка длинный рукав 1 шт", price=47100
)


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A private `TMPDIR`; short on purpose (Chromium's socket path limit)."""
    with tempfile.TemporaryDirectory(prefix="ozb-", dir="/tmp") as directory:
        monkeypatch.setenv("TMPDIR", directory)
        yield directory


def _provider(
    stub: OzonAntibotStub, *, token_timeout_seconds: float = _FUSE
) -> BrowserOzonAccessProvider:
    return BrowserOzonAccessProvider(
        lambda: fetch_ozon_access(
            sandbox=False, home_url=stub.base_url + "/", cookie_domain="127.0.0.1"
        ),
        token_timeout_seconds=token_timeout_seconds,
        failure_pause_seconds=300.0,
        launch_budget=8,
        launch_window_seconds=900.0,
    )


async def _wait_for_home_hit(stub: OzonAntibotStub) -> None:
    reached = await asyncio.to_thread(stub.home_hit.wait, _FUSE)
    assert reached, "the browser never opened the stand-in home page"


@pytest.mark.asyncio
async def test_chain_client_provider_browser_cookies_price(
    private_tmp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) client -> provider -> browser -> cookies -> `200` -> price. The API
    request carries the token the page set (and the earlier cookies) and the
    UA of the browser; the browser saw an ordinary Russian Chrome (the window
    position flag is proved by the launch arguments in the unit tests: the
    `screenX` a page sees right after load is not stable)."""
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="TRACE")
    with OzonAntibotStub() as stub:
        monkeypatch.setattr(OzonClient, "URL", stub.base_url + "/api")
        provider = _provider(stub)
        session = OzonSession()
        try:
            result = await asyncio.wait_for(
                OzonClient(session, provider).get_product_data(_ARTICLE), _FUSE
            )
        finally:
            await provider.aclose()
            await session.aclose()
            logger.remove(sink)

    assert result == _EXPECTED
    assert stub.api_statuses == [200]
    assert len(stub.issued_tokens) == 1
    request = stub.api_requests[0]
    assert f"__Secure-access-token={stub.issued_tokens[0]}" in request["cookie"]
    assert "__Secure-ETC=etc-early" in request["cookie"]
    assert request["query"] == f"url=%2Fproduct%2F{_ARTICLE}%2F"
    assert request["user_agent"] == stub.home_user_agents[0]
    assert "HeadlessChrome" not in request["user_agent"]
    assert len(stub.reports) == 1
    assert {
        k: v for k, v in stub.reports[0].items() if k in {"webdriver", "language", "tz"}
    } == {
        "webdriver": "false",
        "language": "ru-RU",
        "tz": "Europe/Moscow",
    }
    assert processes_started_by_test(private_tmp) == {}
    # Secret hygiene: the token is on the wire (control above) and in no log line.
    assert lines
    assert all(stub.issued_tokens[0] not in line for line in lines)


@pytest.mark.asyncio
async def test_revoked_cookie_gives_one_new_acquisition_and_two_api_requests(
    private_tmp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) the stand-in revokes the cookie: the next price check gets `307`,
    renews the access once (a second browser pass) and returns data after
    exactly two API requests; the second one carries the new token."""
    with OzonAntibotStub() as stub:
        monkeypatch.setattr(OzonClient, "URL", stub.base_url + "/api")
        provider = _provider(stub)
        session = OzonSession()
        try:
            client = OzonClient(session, provider)
            first = await asyncio.wait_for(client.get_product_data(_ARTICLE), _FUSE)
            stub.revoke_all_tokens()
            requests_before = len(stub.api_requests)
            second = await asyncio.wait_for(client.get_product_data(_ARTICLE), _FUSE)
        finally:
            await provider.aclose()
            await session.aclose()

    assert first == _EXPECTED
    assert second == _EXPECTED
    assert stub.api_statuses == [200, 307, 200]
    assert len(stub.api_requests) - requests_before == 2
    assert len(stub.issued_tokens) == 2
    assert (
        f"__Secure-access-token={stub.issued_tokens[0]}"
        in (stub.api_requests[1]["cookie"])
    )
    assert (
        f"__Secure-access-token={stub.issued_tokens[1]}"
        in (stub.api_requests[2]["cookie"])
    )
    assert processes_started_by_test(private_tmp) == {}


@pytest.mark.asyncio
async def test_unsolved_check_is_challenge_timeout_and_leaves_no_browser(
    private_tmp: str,
) -> None:
    """(c), deadline. The page sets `__Secure-ETC` and `abt_data` and reports
    (so they exist), but never the access token: the acquisition must not
    return on the early cookies, it ends by the deadline, no process remains."""
    with OzonAntibotStub(solve_challenge=False) as stub:
        provider = _provider(stub, token_timeout_seconds=6.0)
        before = processes_started_by_test(private_tmp)
        result = await asyncio.wait_for(provider.get(), _FUSE)
        after = processes_started_by_test(private_tmp)

    assert before == {}
    assert stub.reports  # the script ran past setting the early cookies
    assert result == OzonAccessUnavailable(
        OzonAccessStage.challenge_timeout, "no access within 6 s"
    )
    assert after == {}


@pytest.mark.asyncio
async def test_cancelling_the_fetch_closes_the_browser(private_tmp: str) -> None:
    """(c), cancellation: while the fetch waits the browser is alive (positive
    control); after the cancellation is delivered none is left."""
    with OzonAntibotStub(solve_challenge=False) as stub:
        task = asyncio.create_task(
            fetch_ozon_access(
                sandbox=False,
                home_url=stub.base_url + "/",
                cookie_domain="127.0.0.1",
            )
        )
        await _wait_for_home_hit(stub)
        environments = await observe_chromium_environments(private_tmp)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE)
        after = processes_started_by_test(private_tmp)

    assert environments
    assert after == {}


@pytest.mark.asyncio
async def test_browser_environment_is_the_allowlist_without_bot_settings(
    private_tmp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Chromium of Ozon does not inherit the bot's settings. Markers set by
    the test are absent from every Chromium process; positive control: `TMPDIR`,
    an allow-listed variable, is there with the test's value. The observation
    waits (bounded) for the browser instead of sampling once."""
    monkeypatch.setenv("TG__BOT_TOKEN", "1:ENVIRON-MARKER-DO-NOT-LEAK")
    monkeypatch.setenv("DB__PASSWORD", "ENVIRON-MARKER-DB")
    with OzonAntibotStub(solve_challenge=False) as stub:
        task = asyncio.create_task(
            fetch_ozon_access(
                sandbox=False,
                home_url=stub.base_url + "/",
                cookie_domain="127.0.0.1",
            )
        )
        try:
            environments = await observe_chromium_environments(private_tmp)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _FUSE)

    assert any(env.get("TMPDIR") == private_tmp for env in environments)
    leaked = [
        name
        for env in environments
        for name in env
        if name.startswith(("TG__", "DB__", "REDIS__", "APP__", "WB__", "OZON__"))
    ]
    assert leaked == []
    assert all("ENVIRON-MARKER" not in str(env) for env in environments)
