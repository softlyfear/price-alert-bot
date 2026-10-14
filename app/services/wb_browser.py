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

from playwright.async_api import async_playwright

WB_HOME_URL = "https://www.wildberries.ru/"
TOKEN_COOKIE_NAME = "x_wbaas_token"
_POLL_INTERVAL_SECONDS = 0.25

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
    bounds it (cancellation closes the browser). Playwright errors propagate;
    the provider classifies them.
    """
    async with async_playwright() as playwright:
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
