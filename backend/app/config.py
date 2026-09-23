"""Настройки окружения (секреты, подключения). Читаются из переменных TB_* и файла .env.

Торговые параметры (риск, стратегия, инструменты) живут отдельно — см. app.trading_config.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class RunMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: RunMode = RunMode.PAPER
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://tradebot:tradebot@localhost:5432/tradebot"
    redis_url: str = "redis://localhost:6379/0"

    bybit_testnet: bool = True
    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    bybit_recv_window_ms: int = 5000

    trading_config_path: Path = BACKEND_DIR / "config" / "default.yaml"

    # Панель
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 60
    # Мастер-ключ (Fernet) для шифрования API-ключей, хранимых в БД
    master_key: SecretStr = SecretStr("")
    cors_origins: list[str] = ["http://localhost:5173"]

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    @model_validator(mode="after")
    def _live_requires_mainnet_keys(self) -> "Settings":
        if self.mode is RunMode.LIVE:
            if self.bybit_testnet:
                raise ValueError("mode=live несовместим с bybit_testnet=true")
            if not self.bybit_api_key.get_secret_value() or not (
                self.bybit_api_secret.get_secret_value()
            ):
                raise ValueError("mode=live требует TB_BYBIT_API_KEY и TB_BYBIT_API_SECRET")
            if not self.jwt_secret.get_secret_value():
                raise ValueError("mode=live требует TB_JWT_SECRET для панели")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
