"""Get the Ozon anti-bot cookies with a browser (`PROJECT.md` section 2.14).

A full Chromium (`channel="chromium"`) opens the Ozon home page, the anti-bot
check passes by itself and sets the cookie `__Secure-access-token`; the
cookies of `ozon.ru` and its subdomains are returned together with the UA of
the browser. The browser lives for one acquisition and is closed on every
exit, cancellation included. The deadline is held by the caller
(`BrowserOzonAccessProvider`), not here.

`--window-position=10,42` is what makes the check pass (measured, O5, fact
F2): with the default position (`screenX/screenY` = 10/10) `/abt/result` is
answered with 403 in every configuration tried. It is a heuristic of the
anti-bot and brittle: a growth of `challenge_timeout` in the journal is the
trigger to repeat the spike.

The readiness mark is `__Secure-access-token`, not `__Secure-ETC` or
`abt_data`: those exist before the check is passed, so waiting for them would
return unusable cookies.

The environment of the browser process is the allow-list of `browser_launch`.

With a proxy (`PROJECT.md` section 2.14, O10, R2/R3) the proxy is first
pre-checked (handshake, authorisation, `CONNECT` to the host of `home_url`
through it): a refusal is `proxy_failed` and Chromium is not started. Then the
browser goes through a local `SocksRelay` that lives exactly as long as the
browser. A proxy failing after the pre-check is not translated into
`proxy_failed` (named limit of the scheme): a refusal on the navigation is
`launch_failed`, a later one ends as `challenge_timeout`. Without a proxy the
path is unchanged: no relay, no pre-check.
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
    """True for `domain` itself and its subdomains (a leading dot ignored)."""
    host = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


async def fetch_ozon_access(
    *,
    sandbox: bool,
    home_url: str = OZON_HOME_URL,
    cookie_domain: str = OZON_COOKIE_DOMAIN,
    proxy: SocksProxy | None = None,
) -> OzonAccess:
    """Return the cookies of `cookie_domain` set by the Ozon check, and the UA.

    Waits until `__Secure-access-token` appears (non-empty, in the domain) and
    has no deadline of its own: the caller bounds it (cancellation closes the
    browser). A Playwright failure becomes
    `OzonAccessFetchError(launch_failed, <detail constant or class name>)`,
    chained to the original; the exception text is never copied. With a
    `proxy`, a refusal of the pre-check is
    `OzonAccessFetchError(proxy_failed, <DETAIL_PROXY_* constant>)`.
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
            return await _fetch_access(
                sandbox=sandbox,
                home_url=home_url,
                cookie_domain=cookie_domain,
                proxy_url=relay.url,
            )
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
