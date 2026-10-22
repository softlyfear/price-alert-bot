"""Real Chromium against a local anti-bot stand-in (PAB-079 AC5).

`fetch_wb_token` drives a real Chromium (`sandbox=False`: the sandbox does not
start on the development host) against `WbAntibotStub` on `127.0.0.1`; nothing
here talks to Wildberries or any other host. A missing Chromium is a red test,
not a skip - the gate installs it (`make install-browser` /
`PROJECT.md` section 2.12).

No Playwright object is mocked: every branch of `wb_browser.py` is reached on
the stand-in.

Processes of the browser are found by `TMPDIR`: the test points it at a
private directory (an allow-listed variable, passed to the browser), and
Playwright puts the browser profile there, so a process whose command line
holds that directory is one this test started - orphans included.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from loguru import logger
from playwright.async_api import Error as PlaywrightError

from app.schemas.marketplace import MarketplaceProductData
from app.services.wb_access import BrowserWbAccessProvider
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbAccessUnavailable
from app.services.wb_access import WbTokenFetchError
from app.services.wb_browser import fetch_wb_token
from app.services.wb_client import WbClient
from tests.integration.browser_probe import observe_chromium_environments
from tests.integration.wb_antibot_stub import WbAntibotStub

_ARTICLE = 860043555
_FUSE_SECONDS = 60.0
# The major version of the pinned Chromium, from the package's own manifest.
_CHROMIUM_MAJOR = "156"


def _processes_started_by_test(marker: str) -> dict[int, str]:
    """pid -> command line of every live process whose command line holds `marker`."""
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace")
        if marker in cmdline:
            found[int(entry.name)] = cmdline
    return found


@pytest.fixture
def private_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A private `TMPDIR`: the marker that identifies this test's processes.

    Short on purpose: Chromium's singleton socket lives under it and a socket
    path is limited to ~100 bytes (pytest's `tmp_path` is too long).
    """
    with tempfile.TemporaryDirectory(prefix="wbb-", dir="/tmp") as directory:
        monkeypatch.setenv("TMPDIR", directory)
        yield directory


async def _wait_for_home_hit(stub: WbAntibotStub) -> None:
    reached = await asyncio.to_thread(stub.home_hit.wait, _FUSE_SECONDS)
    assert reached, "the browser never opened the stand-in home page"


@pytest.mark.asyncio
async def test_fetch_wb_token_returns_exactly_the_token_the_page_set(
    private_tmp: str,
) -> None:
    with WbAntibotStub() as stub:
        token = await asyncio.wait_for(
            fetch_wb_token(sandbox=False, home_url=stub.base_url + "/"),
            _FUSE_SECONDS,
        )

    assert len(stub.issued_tokens) == 1
    assert token == stub.issued_tokens[0]
    assert re.fullmatch(r"tok-[0-9a-f]{32}", token)
    assert _processes_started_by_test(private_tmp) == {}


@pytest.mark.asyncio
async def test_browser_looks_like_an_ordinary_russian_chrome_to_the_page(
    private_tmp: str,
) -> None:
    """The user agent is a plain Chrome of the pinned major version (no
    `Headless`), `navigator.webdriver` is false, the locale and the time zone
    are the Russian ones. The expected major is a literal from the pinned
    Chromium manifest (`browsers.json`: 156.0.8078.4)."""
    with WbAntibotStub() as stub:
        await asyncio.wait_for(
            fetch_wb_token(sandbox=False, home_url=stub.base_url + "/"),
            _FUSE_SECONDS,
        )

    assert stub.home_user_agents == [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{_CHROMIUM_MAJOR}.0.0.0 Safari/537.36"
    ]
    assert stub.reports == [
        {"webdriver": "false", "language": "ru-RU", "tz": "Europe/Moscow"}
    ]


@pytest.mark.asyncio
async def test_chain_recovers_from_a_spent_token_through_a_new_browser_pass(
    private_tmp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5(b): provider + real fetcher + `WbClient` against the stand-in's
    API: the first request carries the cookie and `deviceid` (`200`); the
    token is then revoked, the next check gets `498`, invalidates, acquires
    through a second browser pass and gets the price of the fixture."""
    with WbAntibotStub() as stub:
        monkeypatch.setattr(WbClient, "URL", stub.base_url + "/api")
        provider = BrowserWbAccessProvider(
            lambda: fetch_wb_token(sandbox=False, home_url=stub.base_url + "/"),
            token_timeout_seconds=_FUSE_SECONDS,
            failure_pause_seconds=300.0,
            launch_budget=8,
            launch_window_seconds=900.0,
        )
        try:
            async with httpx.AsyncClient() as http:
                client = WbClient(http, provider)
                first = await asyncio.wait_for(
                    client.get_product_data(_ARTICLE), _FUSE_SECONDS
                )
                stub.revoke_all_tokens()
                second = await asyncio.wait_for(
                    client.get_product_data(_ARTICLE), _FUSE_SECONDS
                )
        finally:
            await provider.aclose()

    expected = MarketplaceProductData(
        name="Кроссовки женские на высокой подошве", price=142900
    )
    assert first == expected
    assert second == expected
    assert stub.api_statuses == [200, 498, 200]
    assert len(stub.issued_tokens) == 2
    assert [r["cookie"] for r in stub.api_requests] == [
        f"x_wbaas_token={stub.issued_tokens[0]}",
        f"x_wbaas_token={stub.issued_tokens[0]}",
        f"x_wbaas_token={stub.issued_tokens[1]}",
    ]
    assert re.fullmatch(r"site_[0-9a-f]{32}", stub.api_requests[0]["deviceid"])
    assert stub.api_requests[2]["deviceid"] != stub.api_requests[0]["deviceid"]
    assert _processes_started_by_test(private_tmp) == {}


@pytest.mark.asyncio
async def test_unsolved_check_is_challenge_timeout_and_leaves_no_browser(
    private_tmp: str,
) -> None:
    """AC5(c), deadline: the page never sets the cookie; the provider's
    deadline ends the pass as `challenge_timeout`, and afterwards no process
    of this test's browser is alive."""
    with WbAntibotStub(solve_challenge=False) as stub:
        provider = BrowserWbAccessProvider(
            lambda: fetch_wb_token(sandbox=False, home_url=stub.base_url + "/"),
            token_timeout_seconds=4.0,
            failure_pause_seconds=300.0,
            launch_budget=8,
            launch_window_seconds=900.0,
        )
        before = _processes_started_by_test(private_tmp)
        result = await asyncio.wait_for(provider.get(), _FUSE_SECONDS)
        after = _processes_started_by_test(private_tmp)

    assert before == {}
    assert stub.home_hit.is_set()  # the browser did start and reach the page
    assert result == WbAccessUnavailable(
        WbAccessStage.challenge_timeout, "no token within 4 s"
    )
    assert after == {}


@pytest.mark.asyncio
async def test_cancelling_the_fetch_closes_the_browser(private_tmp: str) -> None:
    """AC5(c), cancellation: while the fetch waits for the cookie the browser
    is alive (positive control: its processes are visible); after the
    cancellation is delivered none is left."""
    with WbAntibotStub(solve_challenge=False) as stub:
        task = asyncio.create_task(
            fetch_wb_token(sandbox=False, home_url=stub.base_url + "/")
        )
        await _wait_for_home_hit(stub)
        during = _processes_started_by_test(private_tmp)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, _FUSE_SECONDS)
        after = _processes_started_by_test(private_tmp)

    assert any("chrom" in cmdline for cmdline in during.values())
    assert after == {}


@pytest.mark.asyncio
async def test_browser_environment_is_the_allowlist_without_bot_settings(
    private_tmp: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5(d): the processes of Chromium do not inherit the bot's settings.
    A marker `TG__BOT_TOKEN`-like variable set by the test is absent from the
    environment of every Chromium process; positive control: `TMPDIR`, an
    allow-listed variable, is there with the test's value."""
    monkeypatch.setenv("TG__BOT_TOKEN", "1:ENVIRON-MARKER-DO-NOT-LEAK")
    monkeypatch.setenv("DB__PASSWORD", "ENVIRON-MARKER-DB")
    with WbAntibotStub(solve_challenge=False) as stub:
        task = asyncio.create_task(
            fetch_wb_token(sandbox=False, home_url=stub.base_url + "/")
        )
        try:
            await _wait_for_home_hit(stub)
            # PAB-110 AC12: wait (bounded) for a Chromium process with a
            # readable environment instead of sampling the process list once.
            environs = await observe_chromium_environments(private_tmp)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, _FUSE_SECONDS)

    assert environs, "no Chromium process was found to inspect"
    assert any(env.get("TMPDIR") == private_tmp for env in environs)
    leaked = [
        name
        for env in environs
        for name in env
        if name.startswith(("TG__", "DB__", "REDIS__", "APP__", "WB__"))
    ]
    assert leaked == []
    assert all(
        "ENVIRON-MARKER" not in json.dumps(env, ensure_ascii=False) for env in environs
    )


def _closed_local_port() -> int:
    """A port nothing listens on: bound to 0 by the OS, then released.

    Not port 9: Chromium refuses it as `ERR_UNSAFE_PORT`, a different failure.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.mark.asyncio
async def test_missing_browser_is_browser_not_installed_without_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAB-080 AC9: an empty browsers directory -> `browser_not_installed`,
    raised before the launch, so there is no Playwright exception to chain."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))

    with pytest.raises(WbTokenFetchError) as caught:
        await asyncio.wait_for(fetch_wb_token(sandbox=False), _FUSE_SECONDS)

    assert caught.value.stage is WbAccessStage.launch_failed
    assert caught.value.detail == "browser_not_installed"
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_navigation_to_closed_port_is_navigation_network_error(
    private_tmp: str,
) -> None:
    """PAB-080 AC9: `goto` on a closed `127.0.0.1` port -> `navigation_network_error`,
    chained to the Playwright error; its text is in neither `detail` nor a log
    (positive control: the text does hold the URL and the net error)."""
    url = f"http://127.0.0.1:{_closed_local_port()}/"
    lines: list[str] = []
    handler = logger.add(
        lambda m: lines.append(str(m).rstrip("\n")), level="TRACE", format="{message}"
    )
    try:
        with pytest.raises(WbTokenFetchError) as caught:
            await asyncio.wait_for(
                fetch_wb_token(sandbox=False, home_url=url), _FUSE_SECONDS
            )
        logger.info("control")
    finally:
        logger.remove(handler)

    cause = caught.value.__cause__
    assert caught.value.stage is WbAccessStage.launch_failed
    assert caught.value.detail == "navigation_network_error"
    assert isinstance(cause, PlaywrightError)
    assert url in str(cause)  # positive control: the text carries the URL
    assert url not in caught.value.detail
    assert lines == ["control"]
    assert _processes_started_by_test(private_tmp) == {}
