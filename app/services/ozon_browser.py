"""Acquire Ozon access in Chromium; the caller sets the deadline.

Only __Secure-access-token signals a passed challenge: other cookies appear
earlier. The window position is an anti-bot heuristic observed to work.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.services.browser_launch import DETAIL_BROWSER_NOT_INSTALLED
from app.services.browser_launch import browser_env
from app.services.browser_launch import classify_failure
from app.services.browser_launch import user_agent
from app.services.ozon_access import MaskedStr
from app.services.ozon_access import OzonAccess
from app.services.ozon_access import OzonAccessFetchError
from app.services.ozon_access import OzonAccessStage
from app.services.socks_relay import SocksError
from app.services.socks_relay import SocksProxy
from app.services.socks_relay import SocksRelay
from app.services.socks_relay import check_proxy

OZON_HOME_URL = "https://www.ozon.ru/"
OZON_COOKIE_DOMAIN = "ozon.ru"
READY_COOKIE_NAME = "__Secure-access-token"
_POLL_INTERVAL_SECONDS = 0.25
_CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--window-position=10,42",
]


def _in_domain(cookie_domain: str, domain: str) -> bool:
    host = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


async def fetch_ozon_access(
    *,
    sandbox: bool,
    home_url: str = OZON_HOME_URL,
    cookie_domain: str = OZON_COOKIE_DOMAIN,
    proxy: SocksProxy | None = None,
) -> OzonAccess:
    """Close the browser on cancellation and classify proxy failures separately.

    A proxy is pre-checked before launch; a relay refusal that breaks Playwright
    is reported as proxy_failed. Exception text is never copied into the detail.
    """
    try:
        if proxy is None:
            return await _fetch_access(
                sandbox=sandbox, home_url=home_url, cookie_domain=cookie_domain
            )
        try:
            await check_proxy(proxy, urlsplit(home_url).hostname or "")
        except SocksError as e:
            raise OzonAccessFetchError(OzonAccessStage.proxy_failed, e.detail) from e
        async with SocksRelay(proxy) as relay:
            try:
                return await _fetch_access(
                    sandbox=sandbox,
                    home_url=home_url,
                    cookie_domain=cookie_domain,
                    proxy_url=relay.url,
                )
            except PlaywrightError as e:
                if relay.upstream_failure is None:
                    raise
                raise OzonAccessFetchError(
                    OzonAccessStage.proxy_failed, relay.upstream_failure
                ) from e
    except PlaywrightError as e:
        raise OzonAccessFetchError(
            OzonAccessStage.launch_failed, classify_failure(e)
        ) from e


async def _fetch_access(
    *,
    sandbox: bool,
    home_url: str,
    cookie_domain: str,
    proxy_url: str | None = None,
) -> OzonAccess:
    async with async_playwright() as playwright:
        # Structural signal: the executable is checked before the launch, so
        # "not installed" does not depend on the message text.
        if not await asyncio.to_thread(
            Path(playwright.chromium.executable_path).is_file
        ):
            raise OzonAccessFetchError(
                OzonAccessStage.launch_failed, DETAIL_BROWSER_NOT_INSTALLED
            )
        browser = await playwright.chromium.launch(
            channel="chromium",
            headless=True,
            args=_CHROMIUM_ARGS,
            chromium_sandbox=sandbox,
            env=browser_env(),
            proxy={"server": proxy_url} if proxy_url is not None else None,
        )
        try:
            agent = user_agent(browser.version)
            context = await browser.new_context(
                user_agent=agent,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
            )
            page = await context.new_page()
            await page.goto(home_url, wait_until="commit")
            while True:
                found = [
                    (cookie["name"], MaskedStr(cookie["value"]))
                    for cookie in await context.cookies()
                    if _in_domain(cookie["domain"], cookie_domain)
                ]
                if any(name == READY_COOKIE_NAME and value for name, value in found):
                    return OzonAccess(cookies=tuple(found), user_agent=agent)
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        finally:
            await browser.close()
