"""Application configuration settings."""

from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseSettings):
    """Database connection and pool configuration."""

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
        return (
            f"postgresql+asyncpg://"
            f"{self.USER}:{self.PASSWORD}"
            f"@{self.HOST}:{self.PORT}/{self.NAME}"
        )

    model_config = {
        "env_prefix": "DB_",
        "env_file": BASE_DIR / ".env",
        "extra": "ignore",
    }


class BotSecret(BaseSettings):
    """Telegram bot configuration."""

    bot_token: str = ""

    model_config = {
        "env_prefix": "TG_",
    }


class Settings(BaseSettings):
    """All settings for import."""

    db: DatabaseSettings = DatabaseSettings()
    tg: BotSecret = BotSecret()

    model_config = {
        "env_file": BASE_DIR / ".env",
    }


settings = Settings()
