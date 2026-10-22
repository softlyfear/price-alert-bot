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
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.services.browser_launch import DETAIL_BROWSER_NOT_INSTALLED
from app.services.browser_launch import browser_env
from app.services.browser_launch import classify_failure
from app.services.browser_launch import user_agent
from app.services.wb_access import WbAccessStage
from app.services.wb_access import WbTokenFetchError

WB_HOME_URL = "https://www.wildberries.ru/"
TOKEN_COOKIE_NAME = "x_wbaas_token"
_POLL_INTERVAL_SECONDS = 0.25


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
        raise WbTokenFetchError(WbAccessStage.launch_failed, classify_failure(e)) from e


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
            env=browser_env(),
        )
        try:
            context = await browser.new_context(
                user_agent=user_agent(browser.version),
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
