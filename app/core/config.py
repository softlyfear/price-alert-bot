"""Application configuration settings."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseModel):
    """Local SQLite database and session configuration."""

    PATH: Path = Path("./data/price-alert-bot.sqlite3")
    BUSY_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)
    ECHO: bool = False
    AUTOFLUSH: bool = False
    EXPIRE_ON_COMMIT: bool = False

    @field_validator("PATH", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> object:
        """Require a persistent file name, not an empty or in-memory database."""
        if isinstance(value, str) and not value.strip():
            raise ValueError("PATH must name a database file")
        if isinstance(value, str | Path) and (
            not Path(value).name or str(value) == ":memory:"
        ):
            raise ValueError("PATH must name a persistent database file")
        return value

    @property
    def DATABASE_URL(self) -> str:
        """Construct the SQLite connection URL without URI interpolation."""
        return URL.create(
            drivername="sqlite+aiosqlite", database=str(self.PATH)
        ).render_as_string(hide_password=False)


class BotSecret(BaseModel):
    """Telegram bot configuration."""

    BOT_TOKEN: SecretStr


class RedisSettings(BaseModel):
    """Redis connection configuration for aiogram's RedisStorage (FSM state).

    HOST has no default on purpose: a storage address depends on the
    deployment, and a silent fallback to localhost in production is worse
    than an explicit failure on startup.
    """

    HOST: str = Field(min_length=1)
    PORT: int = Field(default=6379, ge=1, le=65535)
    DB: int = Field(default=0, ge=0)
    PASSWORD: SecretStr | None = None

    @field_validator("HOST")
    @classmethod
    def _reject_blank_host(cls, value: str) -> str:
        """Reject a host made up entirely of whitespace.

        ``min_length=1`` only counts characters, so ``" "`` and ``"  \t "``
        both satisfy it while carrying no real address. That defeats the
        field's purpose: an explicit startup failure instead of a silent
        fallback deep inside ``RedisStorage``.
        """
        if not value.strip():
            raise ValueError("HOST must not be blank")
        return value

    @property
    def REDIS_URL(self) -> str:
        """Construct Redis connection URL.

        The result may contain a password in plain text and must never be
        logged or otherwise exposed.
        """
        password = self.PASSWORD.get_secret_value() if self.PASSWORD is not None else ""
        auth = f":{quote(password, safe='')}@" if password else ""
        return f"redis://{auth}{self.HOST}:{self.PORT}/{self.DB}"


class AppSettings(BaseModel):
    """Application-level runtime configuration.

    None of these values are secrets, so every field has a safe default and
    the application must be able to start without any APP__* variable set.
    """

    SCHEDULER_INTERVAL_SECONDS: int = Field(default=900, gt=0)
    ALERT_COOLDOWN_SECONDS: int = Field(default=86400, gt=0)
    MAX_PRODUCTS_PER_USER: int = Field(default=50, gt=0)
    MARKETPLACE_CONCURRENCY: int = Field(default=5, gt=0)
    HTTP_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    DISPLAY_TIMEZONE: ZoneInfo = ZoneInfo("Europe/Moscow")

    @field_validator("DISPLAY_TIMEZONE", mode="before")
    @classmethod
    def _parse_timezone(cls, value: object) -> object:
        """Turn an IANA name into ``ZoneInfo``; reject blank and unknown names."""
        if not isinstance(value, str):
            return value
        try:
            return ZoneInfo(value.strip())
        except (ValueError, LookupError, OSError) as exc:
            msg = "unknown IANA timezone name"
            raise ValueError(msg) from exc


class WbSettings(BaseModel):
    """Wildberries antibot token acquisition through a browser (PAB-079, Р12).

    None of these values are secrets; the defaults are the measured numbers.
    BROWSER_SANDBOX is off by default: on hosts that restrict unprivileged
    user namespaces the Chromium sandbox does not start.
    """

    TOKEN_TIMEOUT_SECONDS: float = Field(
        default=30.0, gt=0, le=120, allow_inf_nan=False
    )
    FAILURE_PAUSE_SECONDS: float = Field(
        default=300.0, gt=0, le=3600, allow_inf_nan=False
    )
    LAUNCH_BUDGET: int = Field(default=8, ge=1, le=100)
    LAUNCH_WINDOW_SECONDS: float = Field(
        default=900.0, gt=0, le=86400, allow_inf_nan=False
    )
    BROWSER_SANDBOX: bool = False


_PROXY_SCHEMES = frozenset({"socks5", "socks5h"})
_PROXY_CREDENTIAL_MAX_BYTES = 255


def _check_proxy_url(url: str) -> None:
    """Raise a generic ``ValueError`` unless ``url`` is a valid SOCKS5 proxy URL.

    Messages never contain a part of the URL: it holds the proxy host and
    credentials, and a library message such as the one for a non-numeric port
    would echo them.
    """
    msg = "must be socks5://[login:password@]host:port or socks5h://..., no path"
    if "?" in url or "#" in url:
        raise ValueError(msg)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        login = parts.username
        password = parts.password
    except ValueError:
        raise ValueError(msg) from None
    if parts.scheme not in _PROXY_SCHEMES or not host or not host.strip():
        raise ValueError(msg)
    if port is None or not 1 <= port <= 65535:
        raise ValueError(msg)
    if parts.path not in ("", "/"):
        raise ValueError(msg)
    if (login is None) != (password is None):
        raise ValueError(msg)
    for credential in (login, password):
        if credential is None:
            continue
        try:
            size = len(unquote(credential, errors="strict").encode("utf-8"))
        except UnicodeError:
            raise ValueError(msg) from None
        if not 1 <= size <= _PROXY_CREDENTIAL_MAX_BYTES:
            raise ValueError(msg)


class OzonSettings(BaseModel):
    """Ozon cookies acquisition through a browser (PAB-110, PROJECT.md 2.14 O8).

    PROXY_URL (PAB-111, O10) is the only secret: the URL holds the proxy host
    and credentials. Empty means no proxy. The URL rules live here and only
    here (R1); ``SocksProxy.from_url`` just splits an already checked URL.
    Otherwise the shape and defaults are those of ``WbSettings``.
    BROWSER_SANDBOX is off by default for the same reason (restricted user
    namespaces).
    """

    PROXY_URL: SecretStr | None = None

    TOKEN_TIMEOUT_SECONDS: float = Field(
        default=30.0, gt=0, le=120, allow_inf_nan=False
    )
    FAILURE_PAUSE_SECONDS: float = Field(
        default=300.0, gt=0, le=3600, allow_inf_nan=False
    )
    LAUNCH_BUDGET: int = Field(default=8, ge=1, le=100)
    LAUNCH_WINDOW_SECONDS: float = Field(
        default=900.0, gt=0, le=86400, allow_inf_nan=False
    )
    BROWSER_SANDBOX: bool = False

    @field_validator("PROXY_URL", mode="before")
    @classmethod
    def _blank_proxy_is_none(cls, value: object) -> object:
        """Treat an empty or whitespace-only value as "no proxy"."""
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("PROXY_URL")
    @classmethod
    def _validate_proxy_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Check the SOCKS5 URL rules (O10); the message carries no URL part."""
        if value is None:
            return None
        url = value.get_secret_value().strip()
        _check_proxy_url(url)
        return SecretStr(url)


class Settings(BaseSettings):
    """All settings for import."""

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    tg: BotSecret
    redis: RedisSettings
    app: AppSettings = Field(default_factory=AppSettings)
    wb: WbSettings = Field(default_factory=WbSettings)
    ozon: OzonSettings = Field(default_factory=OzonSettings)

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_nested_delimiter="__",
        extra="ignore",
        # Pydantic's "missing field" error echoes the *entire* raw input
        # mapping of the model that failed as validation context -- for a
        # nested model this includes sibling fields that validated fine,
        # secrets among them, and it does so *before* any field is coerced
        # to SecretStr. This is not hypothetical: an unset REDIS__HOST with
        # REDIS__PASSWORD set prints the plain-text password inside
        # ValidationError. SecretStr alone does not close this path because
        # the leaking value is the raw pre-validation input, not a
        # validated field; only suppressing input echo at this outermost
        # model (where validate_python is actually invoked) closes it --
        # setting it on a nested model has no effect, verified empirically.
        hide_input_in_errors=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Lazy settings getter."""
    return Settings()  # pyright: ignore[reportCallIssue]
