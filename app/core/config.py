"""Application configuration settings."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseModel):
    """Database connection and pool configuration."""

    DRIVER: str = "postgresql+asyncpg"
    USER: str
    PASSWORD: SecretStr
    HOST: str
    PORT: int
    NAME: str

    ECHO: bool
    POOL_SIZE: int
    MAX_OVERFLOW: int
    POOL_PRE_PING: bool
    POOL_RECYCLE: int

    AUTOFLUSH: bool
    EXPIRE_ON_COMMIT: bool

    @property
    def DATABASE_URL(self) -> str:
        """Construct PostgreSQL connection URL."""
        return URL.create(
            drivername=self.DRIVER,
            username=self.USER,
            password=self.PASSWORD.get_secret_value(),
            host=self.HOST,
            port=self.PORT,
            database=self.NAME,
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


class Settings(BaseSettings):
    """All settings for import."""

    db: DatabaseSettings
    tg: BotSecret
    redis: RedisSettings
    app: AppSettings = Field(default_factory=AppSettings)

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
