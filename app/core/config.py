"""Application configuration settings."""

from pydantic_settings import BaseSettings
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


class DataBaseUrl(BaseSettings):
    """Database connection and pool configuration."""

    url: str = "sqlite+aiosqlite:///users.db"
    echo: bool = False

    autoflush: bool = False
    expire_on_commit: bool = False

    model_config = {
        "env_prefix": "BD_",
    }


class BotSecret(BaseSettings):
    """Telegram bot configuration."""

    bot_token: str = ""

    model_config = {
        "env_prefix": "TG_",
    }


class Settings(BaseSettings):
    """All settings for import."""

    bd: DataBaseUrl = DataBaseUrl()
    tg: BotSecret = BotSecret()

    model_config = {
        "env_file": BASE_DIR / ".env",
    }


settings = Settings()
