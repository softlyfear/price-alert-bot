"""Get the Wildberries anti-bot token with a browser (`PROJECT.md` section 2.12).

The only module of the project that imports `playwright` (import-linter
contract L6). A full Chromium (`channel="chromium"`; the headless shell gets a
token WB rejects with 498 - measured, `PROJECT.md` section 10.0) opens the WB
home page, the anti-bot check passes by itself and sets the cookie
`x_wbaas_token`; the cookie value is returned. The browser lives for one
acquisition and is closed on every exit, cancellation included. The deadline
is held by the caller (`BrowserWbAccessProvider`), not here.

The browser process gets a minimal environment (an allow-list), so the bot's
own settings and secrets are not inherited by a process that executes
third-party JavaScript. The Playwright driver (Node.js) is our code and
inherits the bot's environment.
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbTokenFetchError

WB_HOME_URL = "https://www.wildberries.ru/"
TOKEN_COOKIE_NAME = "x_wbaas_token"
_POLL_INTERVAL_SECONDS = 0.25

# Closed set of `detail` values of a Playwright failure (stage `launch_failed`);
# anything else is reported by the exception class name only.
DETAIL_BROWSER_NOT_INSTALLED = "browser_not_installed"
DETAIL_SANDBOX_UNAVAILABLE = "sandbox_unavailable"
DETAIL_NAVIGATION_NETWORK_ERROR = "navigation_network_error"

# Verbatim fragments of Playwright messages, observed with Playwright 1.64 /
# Chromium 1248 (the exception type alone does not tell the cases apart):
# a launch with a sandbox the host forbids is `TargetClosedError` carrying the
# browser log "Chromium sandboxing failed!"; a failed `goto` is `Error`
# "Page.goto: net::ERR_CONNECTION_REFUSED at ...". The text is only matched,
# never copied into `detail` or a log (it embeds paths, URLs and a browser log).
_SANDBOX_FAILED_MARKER = "Chromium sandboxing failed"
_NETWORK_ERROR_MARKER = "net::ERR_"

# Names copied from the bot's environment into the browser's one.
_BROWSER_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def _browser_env() -> dict[str, str | float | bool]:
    return {
        name: os.environ[name] for name in _BROWSER_ENV_ALLOWLIST if name in os.environ
    }


def _classify_failure(error: PlaywrightError) -> str:
    """Map a Playwright error to a `detail` constant, else the class name."""
    message = str(error)
    if _SANDBOX_FAILED_MARKER in message:
        return DETAIL_SANDBOX_UNAVAILABLE
    if _NETWORK_ERROR_MARKER in message:
        return DETAIL_NAVIGATION_NETWORK_ERROR
    return type(error).__name__


def _user_agent(browser_version: str) -> str:
    """Ordinary Chrome UA (no `Headless`) with the browser's own major version."""
    major = browser_version.split(".", 1)[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


async def fetch_wb_token(*, sandbox: bool, home_url: str = WB_HOME_URL) -> str:
    """Return the value of the `x_wbaas_token` cookie set by the WB check.

    Waits until the cookie appears and has no deadline of its own: the caller
    bounds it (cancellation closes the browser). A Playwright failure becomes
    `WbTokenFetchError(launch_failed, <detail constant or class name>)`,
    chained to the original; the exception text is never copied.
    """
    try:
        return await _fetch_token(sandbox=sandbox, home_url=home_url)
    except PlaywrightError as e:
        raise WbTokenFetchError(
            WbAccessStage.launch_failed, _classify_failure(e)
        ) from e


async def _fetch_token(*, sandbox: bool, home_url: str) -> str:
    async with async_playwright() as playwright:
        # Structural signal: the executable is checked before the launch, so
        # "not installed" does not depend on the message text.
        if not await asyncio.to_thread(
            Path(playwright.chromium.executable_path).is_file
        ):
            raise WbTokenFetchError(
                WbAccessStage.launch_failed, DETAIL_BROWSER_NOT_INSTALLED
            )
        browser = await playwright.chromium.launch(
            channel="chromium",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            chromium_sandbox=sandbox,
            env=_browser_env(),
        )
        try:
            context = await browser.new_context(
                user_agent=_user_agent(browser.version),
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = await context.new_page()
            await page.goto(home_url, wait_until="commit")
            while True:
                for cookie in await context.cookies():
                    if cookie["name"] == TOKEN_COOKIE_NAME and cookie["value"]:
                        return cookie["value"]
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            await browser.close()
