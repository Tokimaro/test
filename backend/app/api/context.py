"""Общие зависимости API: контекст приложения, авторизация, ограничение попыток входа."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.security import SecretBox, TokenService
from app.config import Settings
from app.core.events import EventBus
from app.core.runner import BotRuntime
from app.db.repo import TradeRepo
from app.trading_config import TradingConfig


class LoginLimiter:
    """Ограничение подбора пароля.

    Основной счётчик — по паре (логин, IP): посторонний с другого адреса не может
    заблокировать владельца. Дополнительный, более мягкий, — по IP (перебор логинов).
    Размер таблицы ограничен, пустые записи удаляются."""

    MAX_KEYS = 10_000

    def __init__(
        self, max_failures: int = 5, max_failures_per_ip: int = 20, window_s: int = 900
    ) -> None:
        self.max = max_failures
        self.max_ip = max_failures_per_ip
        self.window = window_s
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> int:
        now = time.monotonic()
        items = [t for t in self._fails.get(key, ()) if now - t < self.window]
        if items:
            self._fails[key] = items
        else:
            self._fails.pop(key, None)
        return len(items)

    def blocked(self, login: str, ip: str) -> bool:
        return self._recent(f"{login}|{ip}") >= self.max or self._recent(f"ip:{ip}") >= self.max_ip

    def fail(self, login: str, ip: str) -> None:
        if len(self._fails) >= self.MAX_KEYS:
            # вытесняем самые старые записи, чтобы память не росла бесконечно
            oldest = sorted(self._fails, key=lambda k: self._fails[k][-1])[: self.MAX_KEYS // 10]
            for k in oldest:
                del self._fails[k]
        now = time.monotonic()
        for key in (f"{login}|{ip}", f"ip:{ip}"):
            self._fails.setdefault(key, []).append(now)

    def reset(self, login: str, ip: str) -> None:
        self._fails.pop(f"{login}|{ip}", None)


@dataclass
class AppContext:
    settings: Settings
    sm: async_sessionmaker[AsyncSession]
    repo: TradeRepo
    bus: EventBus
    tokens: TokenService
    secrets: SecretBox
    config: TradingConfig
    runtime: BotRuntime | None = None
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]

_bearer = HTTPBearer(auto_error=False)


def current_user(
    ctx: Ctx,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    login = ctx.tokens.verify(creds.credentials) if creds is not None else None
    if login is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "требуется вход")
    return login


User = Annotated[str, Depends(current_user)]
