"""Application configuration settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseModel):
    """Database connection and pool configuration."""

    DRIVER: str = "postgresql+asyncpg"
    USER: str
    PASSWORD: str
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
            password=self.PASSWORD,
            host=self.HOST,
            port=self.PORT,
            database=self.NAME,
        ).render_as_string(hide_password=False)


class BotSecret(BaseModel):
    """Telegram bot configuration."""

    BOT_TOKEN: str


class Settings(BaseSettings):
    """All settings for import."""

    db: DatabaseSettings
    tg: BotSecret

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_nested_delimiter="__",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Lazy settings getter."""
    return Settings()  # pyright: ignore[reportCallIssue]
