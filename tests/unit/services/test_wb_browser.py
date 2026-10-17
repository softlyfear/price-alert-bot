"""Unit tests for the failure classifier of `app.services.wb_browser` (PAB-080).

`async_playwright` is replaced at the module boundary by a hand-written fake
whose `chromium.launch` raises a planned exception; nothing starts a browser.
The exception texts are the verbatim shapes observed with Playwright 1.64 /
Chromium 1248. The text of an exception must never reach `detail` or a log
(convention 22): every such cell carries a positive control - the fragment is
in `str(e)` and the log sink does capture records.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from types import TracebackType
from typing import TYPE_CHECKING

import pytest
from loguru import logger

# Not re-exported by `playwright.async_api`; the class the launch really raises.
from playwright._impl._errors import TargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.services import wb_browser
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbTokenFetchError
from app.services.wb_browser import fetch_wb_token

if TYPE_CHECKING:
    from loguru import Message

_SECRET_FRAGMENT = "/home/secret-user/.cache/ms-playwright"


class _FakeChromium:
    def __init__(self, error: BaseException, executable: str) -> None:
        self.executable_path = executable
        self._error = error

    async def launch(self, **_: object) -> object:
        raise self._error


class _FakePlaywright:
    def __init__(self, error: BaseException, executable: str) -> None:
        self.chromium = _FakeChromium(error, executable)


class _FakeManager:
    def __init__(self, error: BaseException, executable: str) -> None:
        self._playwright = _FakePlaywright(error, executable)

    async def __aenter__(self) -> _FakePlaywright:
        return self._playwright

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def _launch_raises(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    executable: str = sys.executable,
) -> None:
    monkeypatch.setattr(
        wb_browser, "async_playwright", lambda: _FakeManager(error, executable)
    )


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    lines: list[str] = []

    def sink(message: Message) -> None:
        lines.append(str(message).rstrip("\n"))

    handler = logger.add(sink, level="TRACE", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler)


async def _fail(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> WbTokenFetchError:
    _launch_raises(monkeypatch, error)
    with pytest.raises(WbTokenFetchError) as caught:
        await fetch_wb_token(sandbox=True)
    return caught.value


@pytest.mark.asyncio
async def test_sandbox_failure_is_sandbox_unavailable_and_text_is_not_copied(
    monkeypatch: pytest.MonkeyPatch, log_lines: list[str]
) -> None:
    error = TargetClosedError(
        "BrowserType.launch: Target page, context or browser has been closed\n"
        "Browser logs:\n"
        f"Chromium sandboxing failed! {_SECRET_FRAGMENT}\n"
    )
    failure = await _fail(monkeypatch, error)
    logger.info("control")

    assert failure.stage is WbAccessStage.launch_failed
    assert failure.detail == "sandbox_unavailable"
    assert failure.__cause__ is error
    assert _SECRET_FRAGMENT in str(error)  # positive control
    assert _SECRET_FRAGMENT not in repr(failure.detail)
    assert log_lines == ["control"]  # the sink works and holds nothing else


@pytest.mark.asyncio
async def test_target_closed_without_sandbox_marker_reports_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(
        monkeypatch,
        TargetClosedError("BrowserType.launch: Target closed\nBrowser logs:\nboom"),
    )

    assert failure.detail == "TargetClosedError"


@pytest.mark.asyncio
async def test_plain_playwright_error_without_markers_reports_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(monkeypatch, PlaywrightError(f"oops {_SECRET_FRAGMENT}"))

    assert failure.detail == "Error"
    assert _SECRET_FRAGMENT not in failure.detail


@pytest.mark.asyncio
async def test_playwright_timeout_reports_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(monkeypatch, PlaywrightTimeoutError("Timeout 30000ms"))

    assert failure.detail == "TimeoutError"


@pytest.mark.asyncio
async def test_network_error_text_is_navigation_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(
        monkeypatch,
        PlaywrightError("Page.goto: net::ERR_CONNECTION_REFUSED at http://x/"),
    )

    assert failure.detail == "navigation_network_error"


@pytest.mark.asyncio
async def test_sandbox_marker_wins_over_network_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = await _fail(
        monkeypatch,
        PlaywrightError("Chromium sandboxing failed! then net::ERR_FAILED"),
    )

    assert failure.detail == "sandbox_unavailable"


@pytest.mark.asyncio
async def test_missing_executable_is_browser_not_installed_without_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_raises(
        monkeypatch, AssertionError("launch must not be reached"), "/nonexistent/chrome"
    )
    with pytest.raises(WbTokenFetchError) as caught:
        await fetch_wb_token(sandbox=True)

    assert caught.value.stage is WbAccessStage.launch_failed
    assert caught.value.detail == "browser_not_installed"
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_non_playwright_error_passes_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_raises(monkeypatch, RuntimeError("bug"))

    with pytest.raises(RuntimeError, match="bug"):
        await fetch_wb_token(sandbox=True)


@pytest.mark.asyncio
async def test_cancellation_passes_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_raises(monkeypatch, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await fetch_wb_token(sandbox=True)
