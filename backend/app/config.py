"""Настройки окружения (секреты, подключения). Читаются из переменных TB_* и файла .env.

Торговые параметры (риск, стратегия, инструменты) живут отдельно — см. app.trading_config.
"""

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator, model_validator
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

    bybit_testnet: bool = True
    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    bybit_recv_window_ms: int = 5000

    trading_config_path: Path = BACKEND_DIR / "config" / "default.yaml"
    # Запускать торговый движок вместе с API (в тестах и для чистого API — false)
    run_bot: bool = True
    paper_initial_equity: Decimal = Decimal(10_000)

    # Панель
    jwt_secret: SecretStr = SecretStr("")
    jwt_ttl_minutes: int = 60
    # Мастер-ключ (Fernet) для шифрования API-ключей, хранимых в БД
    master_key: SecretStr = SecretStr("")
    cors_origins: list[str] = ["http://localhost:5173"]

    # Alpaca (акции США). В режиме paper всегда используется paper-счёт Alpaca;
    # в live — реальный, только если alpaca_paper=false задан явно.
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_paper: bool = True
    alpaca_feed: str = "iex"

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    # В групповом чате команды принимаются только от этих пользователей (id из Telegram)
    telegram_admin_ids: list[int] = []

    @field_validator("master_key")
    @classmethod
    def _valid_master_key(cls, v: SecretStr) -> SecretStr:
        # пробелы, \r из Windows-переводов строк и кавычки вокруг — частые ошибки копирования
        key = v.get_secret_value().strip().strip("'\"")
        if not key:
            return SecretStr("")
        try:
            Fernet(key.encode())
        except ValueError:
            raise ValueError(
                f"TB_MASTER_KEY не является ключом Fernet (длина {len(key)}, нужно 44 символа "
                "с '=' в конце). Сгенерируйте новый: python -c \"from cryptography.fernet "
                'import Fernet; print(Fernet.generate_key().decode())"'
            ) from None
        return SecretStr(key)

    @model_validator(mode="after")
    def _live_requires_mainnet_keys(self) -> "Settings":
        jwt = self.jwt_secret.get_secret_value()
        if jwt and len(jwt) < 32:
            raise ValueError("TB_JWT_SECRET должен быть не короче 32 символов")
        if self.mode is RunMode.LIVE:
            if self.bybit_testnet:
                raise ValueError("mode=live несовместим с bybit_testnet=true")
            if not self.jwt_secret.get_secret_value():
                raise ValueError("mode=live требует TB_JWT_SECRET для панели")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
