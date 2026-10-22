"""Pure parts of a Chromium launch shared by the browser modules.

Moved out of `wb_browser.py` without a change of behaviour when Ozon became
the second consumer (`PROJECT.md` section 2.14, O2): a copy of the browser
environment allow-list in two modules would drift apart on the first edit,
and the list protects the bot's secrets from a process that executes
third-party JavaScript. Imports `playwright` only for the error type
(import-linter contract L6).
"""

import os

from playwright.async_api import Error as PlaywrightError

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


def browser_env() -> dict[str, str | float | bool]:
    """Minimal environment of the browser process (allow-list)."""
    return {
        name: os.environ[name] for name in _BROWSER_ENV_ALLOWLIST if name in os.environ
    }


def classify_failure(error: PlaywrightError) -> str:
    """Map a Playwright error to a `detail` constant, else the class name."""
    message = str(error)
    if _SANDBOX_FAILED_MARKER in message:
        return DETAIL_SANDBOX_UNAVAILABLE
    if _NETWORK_ERROR_MARKER in message:
        return DETAIL_NAVIGATION_NETWORK_ERROR
    return type(error).__name__


def user_agent(browser_version: str) -> str:
    """Ordinary Chrome UA (no `Headless`) with the browser's own major version."""
    major = browser_version.split(".", 1)[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
