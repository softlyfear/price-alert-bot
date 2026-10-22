"""Unit tests for `app.services.ozon_browser.fetch_ozon_access` (PAB-110 AC6, AC13).

`async_playwright` is replaced at the module boundary by a hand-written fake
that records how the browser was launched and plays back planned cookie
snapshots; no browser starts. A real Chromium against a local stand-in is
covered by `tests/integration/test_ozon_chain_live.py`.

Expected values are literals of the ticket (flags, locale, zone, cookie
names), not read from the module. The exception text of Playwright must never
reach `detail` or a log (convention 22): the failure cells carry a positive
control - the fragment is in `str(e)` and the sink captures records.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from types import TracebackType
from typing import Any

import pytest
from loguru import logger

# Not re-exported by `playwright.async_api`; the class the launch really raises.
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError

from app.services import ozon_browser
from app.services.ozon_access import OzonAccessFetchError
from app.services.ozon_access import OzonAccessStage
from app.services.ozon_browser import fetch_ozon_access

_SECRET_FRAGMENT = "/home/secret-user/.cache/ms-playwright"
_FUSE_SECONDS = 5.0

_Cookie = dict[str, str]


def _cookie(name: str, value: str, domain: str = ".ozon.ru") -> _Cookie:
    return {"name": name, "value": value, "domain": domain}


class _FakePage:
    def __init__(
        self, browser: _FakeBrowser, goto_raises: BaseException | None
    ) -> None:
        self._browser = browser
        self._goto_raises = goto_raises

    async def goto(self, url: str, **kwargs: Any) -> None:
        self._browser.goto_calls.append((url, kwargs))
        if self._goto_raises is not None:
            raise self._goto_raises


class _FakeContext:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    async def new_page(self) -> _FakePage:
        return _FakePage(self._browser, self._browser.goto_raises)

    async def cookies(self) -> list[_Cookie]:
        browser = self._browser
        index = min(browser.cookie_reads, len(browser.snapshots) - 1)
        browser.cookie_reads += 1
        browser.owner_cookies_read.set()
        return browser.snapshots[index]


class _FakeBrowser:
    version = "156.0.8078.4"

    def __init__(
        self,
        snapshots: list[list[_Cookie]],
        goto_raises: BaseException | None,
        owner_cookies_read: asyncio.Event,
    ) -> None:
        self.owner_cookies_read = owner_cookies_read
        self.snapshots = snapshots
        self.goto_raises = goto_raises
        self.cookie_reads = 0
        self.context_kwargs: dict[str, Any] = {}
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    async def new_context(self, **kwargs: Any) -> _FakeContext:
        self.context_kwargs = kwargs
        return _FakeContext(self)

    async def close(self) -> None:
        self.closed += 1


class _FakeChromium:
    def __init__(self, owner: _Fake, executable: str) -> None:
        self.executable_path = executable
        self._owner = owner

    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        owner = self._owner
        owner.launch_kwargs = kwargs
        if owner.launch_raises is not None:
            raise owner.launch_raises
        owner.browser = _FakeBrowser(
            owner.snapshots, owner.goto_raises, owner.cookies_read
        )
        return owner.browser


class _Fake:
    """The fake `async_playwright()` manager plus everything it observed."""

    def __init__(
        self,
        snapshots: list[list[_Cookie]] | None = None,
        *,
        launch_raises: BaseException | None = None,
        goto_raises: BaseException | None = None,
        executable: str = sys.executable,
    ) -> None:
        self.snapshots = snapshots or [[_cookie("__Secure-access-token", "T")]]
        self.launch_raises = launch_raises
        self.goto_raises = goto_raises
        self.launch_kwargs: dict[str, Any] = {}
        self.cookies_read = asyncio.Event()
        self.browser: _FakeBrowser | None = None
        self.chromium = _FakeChromium(self, executable)

    async def __aenter__(self) -> _Fake:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _Fake:
    return install(monkeypatch, _Fake())


def install(monkeypatch: pytest.MonkeyPatch, fake: _Fake) -> _Fake:
    monkeypatch.setattr(ozon_browser, "async_playwright", lambda: fake)
    monkeypatch.setattr(ozon_browser, "_POLL_INTERVAL_SECONDS", 0)
    return fake


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    lines: list[str] = []
    handler = logger.add(
        lambda m: lines.append(str(m).rstrip("\n")), level="TRACE", format="{message}"
    )
    try:
        yield lines
    finally:
        logger.remove(handler)


async def _fetch(**kwargs: Any) -> Any:
    return await asyncio.wait_for(fetch_ozon_access(**kwargs), _FUSE_SECONDS)


# --- launch observation (AC6) --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [True, False])
async def test_launch_arguments_are_the_measured_configuration(
    fake: _Fake, sandbox: bool
) -> None:
    await _fetch(sandbox=sandbox)

    kwargs = fake.launch_kwargs
    assert kwargs["channel"] == "chromium"
    assert kwargs["headless"] is True
    assert kwargs["chromium_sandbox"] is sandbox
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
    assert "--window-position=10,42" in kwargs["args"]
    assert len(kwargs["args"]) == 2


@pytest.mark.asyncio
async def test_browser_environment_is_the_allowlist_without_bot_settings(
    fake: _Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TG__BOT_TOKEN", "1:ENVIRON-MARKER-DO-NOT-LEAK")
    monkeypatch.setenv("DB__PASSWORD", "ENVIRON-MARKER-DB")
    monkeypatch.setenv("TZ", "UTC")

    await _fetch(sandbox=True)

    env = fake.launch_kwargs["env"]
    assert env["TZ"] == "UTC"  # positive control: an allow-listed name passes
    assert "PATH" in env
    assert not any(name.startswith(("TG__", "DB__")) for name in env)
    assert "ENVIRON-MARKER" not in str(env)


@pytest.mark.asyncio
async def test_context_is_an_ordinary_russian_chrome_of_the_browser_version(
    fake: _Fake,
) -> None:
    await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.context_kwargs == {
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
        ),
        "locale": "ru-RU",
        "timezone_id": "Europe/Moscow",
    }


@pytest.mark.asyncio
async def test_home_page_is_opened_once_and_the_access_carries_the_context_ua(
    fake: _Fake,
) -> None:
    access = await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.goto_calls == [
        ("https://www.ozon.ru/", {"wait_until": "commit"})
    ]
    assert access.user_agent == fake.browser.context_kwargs["user_agent"]


@pytest.mark.asyncio
async def test_custom_home_url_is_used(fake: _Fake) -> None:
    await _fetch(sandbox=True, home_url="http://127.0.0.1:1/")

    assert fake.browser is not None
    assert fake.browser.goto_calls[0][0] == "http://127.0.0.1:1/"


# --- readiness (AC6) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_readiness_is_the_nonempty_access_token_not_the_earlier_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`__Secure-ETC` and `abt_data` exist before the check is passed: waiting
    for them would return unusable cookies. An empty token is not ready."""
    early = [_cookie("__Secure-ETC", "e"), _cookie("abt_data", "a")]
    empty = [*early, _cookie("__Secure-access-token", "")]
    ready = [*early, _cookie("__Secure-access-token", "TOKEN-MARKER")]
    fake = install(monkeypatch, _Fake([early, empty, ready]))

    access = await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.cookie_reads == 3
    assert dict(access.cookies) == {
        "__Secure-ETC": "e",
        "abt_data": "a",
        "__Secure-access-token": "TOKEN-MARKER",
    }


@pytest.mark.asyncio
async def test_only_ozon_domain_cookies_are_returned_and_count_for_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token of a foreign domain does not make the access ready; of the
    cookies in the ready snapshot only `ozon.ru` and its subdomains remain
    (`evilozon.ru` and `ozon.ru.evil.com` are not subdomains)."""
    foreign_token = [_cookie("__Secure-access-token", "FOREIGN", "evilozon.ru")]
    ready = [
        _cookie("__Secure-access-token", "OK", ".ozon.ru"),
        _cookie("a", "1", "www.ozon.ru"),
        _cookie("b", "2", "OZON.RU"),
        _cookie("c", "3", "evilozon.ru"),
        _cookie("d", "4", "ozon.ru.evil.com"),
        _cookie("e", "5", "example.com"),
    ]
    fake = install(monkeypatch, _Fake([foreign_token, ready]))

    access = await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.cookie_reads == 2
    assert [name for name, _ in access.cookies] == ["__Secure-access-token", "a", "b"]


@pytest.mark.asyncio
async def test_returned_access_masks_cookie_values_in_repr(fake: _Fake) -> None:
    access = await _fetch(sandbox=True)

    assert dict(access.cookies)["__Secure-access-token"] == "T"  # the real value
    assert repr(access.cookies[0][1]) == "'***'"
    assert "'T'" not in repr(access.cookies)


# --- closing -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_is_closed_after_a_successful_acquisition(fake: _Fake) -> None:
    await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.closed == 1


@pytest.mark.asyncio
async def test_browser_is_closed_when_the_fetch_is_cancelled_while_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(monkeypatch, _Fake([[_cookie("__Secure-ETC", "e")]]))
    task = asyncio.create_task(fetch_ozon_access(sandbox=True))
    await asyncio.wait_for(fake.cookies_read.wait(), _FUSE_SECONDS)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, _FUSE_SECONDS)

    assert fake.browser is not None
    assert fake.browser.closed == 1


@pytest.mark.asyncio
async def test_browser_is_closed_when_navigation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        _Fake(goto_raises=PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED")),
    )

    with pytest.raises(OzonAccessFetchError):
        await _fetch(sandbox=True)

    assert fake.browser is not None
    assert fake.browser.closed == 1


# --- failures (AC6) --------------------------------------------------------------


async def _fail(monkeypatch: pytest.MonkeyPatch, **plan: Any) -> OzonAccessFetchError:
    install(monkeypatch, _Fake(**plan))
    with pytest.raises(OzonAccessFetchError) as caught:
        await _fetch(sandbox=True)
    return caught.value


@pytest.mark.asyncio
async def test_sandbox_failure_is_sandbox_unavailable_and_text_is_not_copied(
    monkeypatch: pytest.MonkeyPatch, log_lines: list[str]
) -> None:
    error = TargetClosedError(
        "BrowserType.launch: Target page, context or browser has been closed\n"
        f"Browser logs:\nChromium sandboxing failed! {_SECRET_FRAGMENT}\n"
    )

    failure = await _fail(monkeypatch, launch_raises=error)
    logger.info("control")

    assert failure.stage is OzonAccessStage.launch_failed
    assert failure.detail == "sandbox_unavailable"
    assert failure.__cause__ is error
    assert _SECRET_FRAGMENT in str(error)  # positive control
    assert _SECRET_FRAGMENT not in failure.detail
    assert _SECRET_FRAGMENT not in str(failure)
    assert log_lines == ["control"]


@pytest.mark.asyncio
async def test_navigation_network_error_is_a_constant_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(
        monkeypatch,
        goto_raises=PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED at x"),
    )

    assert failure.detail == "navigation_network_error"


@pytest.mark.asyncio
async def test_plain_playwright_error_reports_class_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(
        monkeypatch, launch_raises=PlaywrightError(f"oops {_SECRET_FRAGMENT}")
    )

    assert failure.stage is OzonAccessStage.launch_failed
    assert failure.detail == "Error"


@pytest.mark.asyncio
async def test_missing_executable_is_browser_not_installed_without_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = install(
        monkeypatch,
        _Fake(
            launch_raises=AssertionError("must not launch"), executable="/nonexistent"
        ),
    )

    with pytest.raises(OzonAccessFetchError) as caught:
        await _fetch(sandbox=True)

    assert caught.value.stage is OzonAccessStage.launch_failed
    assert caught.value.detail == "browser_not_installed"
    assert caught.value.__cause__ is None
    assert fake.launch_kwargs == {}


@pytest.mark.asyncio
async def test_non_playwright_error_passes_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, _Fake(launch_raises=RuntimeError("bug")))

    with pytest.raises(RuntimeError, match="bug"):
        await _fetch(sandbox=True)
