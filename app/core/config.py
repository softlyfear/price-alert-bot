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
    """Redis FSM storage settings; HOST is required for an explicit deployment."""

    HOST: str = Field(min_length=1)
    PORT: int = Field(default=6379, ge=1, le=65535)
    DB: int = Field(default=0, ge=0)
    PASSWORD: SecretStr | None = None

    @field_validator("HOST")
    @classmethod
    def _reject_blank_host(cls, value: str) -> str:
        """Reject whitespace, which satisfies min_length but is not an address."""
        if not value.strip():
            raise ValueError("HOST must not be blank")
        return value

    @property
    def REDIS_URL(self) -> str:
        """The returned URL may contain a plaintext password and must not be logged."""
        password = self.PASSWORD.get_secret_value() if self.PASSWORD is not None else ""
        auth = f":{quote(password, safe='')}@" if password else ""
        return f"redis://{auth}{self.HOST}:{self.PORT}/{self.DB}"


class AppSettings(BaseModel):
    """Non-secret runtime settings with safe defaults."""

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
    """Browser sandbox defaults off for hosts restricting unprivileged namespaces."""

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
    """Validate SOCKS5 URLs without exposing the host or credentials in errors."""
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
    """Browser access settings; PROXY_URL contains credentials when configured."""

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
        """Validate the proxy without exposing its contents in errors."""
        if value is None:
            return None
        url = value.get_secret_value().strip()
        _check_proxy_url(url)
        return SecretStr(url)


class Settings(BaseSettings):
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
        # Nested validation errors may echo raw secrets before SecretStr coercion;
        # suppress input at the outer model where validation is invoked.
        hide_input_in_errors=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Lazy settings getter."""
    return Settings()  # pyright: ignore[reportCallIssue]
