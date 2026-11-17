from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "mysql+pymysql://ama:ama@mysql/ama?charset=utf8mb4"
    bot_id: str = "ama-franchising"
    telegram_token: str = ""
    telegram_webhook_secret: str = ""
    max_token: str = ""
    max_webhook_secret: str = ""
    max_api_url: str = "https://platform-api.max.ru"
    setup_token: str = Field(default="", repr=False)
    public_url: str = ""
    file_root: str = "/data/files"
    max_file_bytes: int = 20 * 1024 * 1024
    worker_interval: float = 2
    consent_text: str = ""
    privacy_url: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()
