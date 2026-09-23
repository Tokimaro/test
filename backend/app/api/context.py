"""Общие зависимости API: контекст приложения, авторизация, ограничение попыток входа."""

import asyncio
import time
from collections import defaultdict
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
    """Не более max_failures неудачных входов за window секунд на логин и на IP."""

    def __init__(self, max_failures: int = 5, window_s: int = 900) -> None:
        self.max = max_failures
        self.window = window_s
        self._fails: dict[str, list[float]] = defaultdict(list)

    def _recent(self, key: str) -> list[float]:
        now = time.monotonic()
        self._fails[key] = [t for t in self._fails[key] if now - t < self.window]
        return self._fails[key]

    def blocked(self, *keys: str) -> bool:
        return any(len(self._recent(k)) >= self.max for k in keys)

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._fails[k].append(time.monotonic())

    def reset(self, *keys: str) -> None:
        for k in keys:
            self._fails.pop(k, None)


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
