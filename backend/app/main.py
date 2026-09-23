from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr

from app import __version__
from app.api import routes, ws
from app.api.context import AppContext
from app.api.security import SecretBox, TokenService
from app.config import BACKEND_DIR, RunMode, Settings, get_settings
from app.core.events import EventBus
from app.core.runner import BotRuntime
from app.db.models import SecretRow
from app.db.repo import TradeRepo
from app.db.session import make_engine, make_sessionmaker
from app.logging import configure_logging
from app.notify.telegram import TelegramNotifier
from app.trading_config import TradingConfig

log = structlog.get_logger()

STATIC_DIR = BACKEND_DIR / "static"  # сюда кладётся собранный фронтенд (npm run build)


async def _with_stored_keys(settings: Settings, ctx: AppContext) -> Settings:
    """Ключи Bybit из переменных окружения важнее; иначе — расшифрованные из БД."""
    if settings.bybit_api_key.get_secret_value() or not ctx.secrets.enabled:
        return settings
    async with ctx.sm() as s:
        key_row = await s.get(SecretRow, routes.BYBIT_KEY_SECRET)
        secret_row = await s.get(SecretRow, routes.BYBIT_SECRET_SECRET)
    if key_row is None or secret_row is None:
        return settings
    key, secret = (
        ctx.secrets.decrypt(key_row.ciphertext),
        ctx.secrets.decrypt(secret_row.ciphertext),
    )
    if not key or not secret:
        return settings
    return settings.model_copy(
        update={"bybit_api_key": SecretStr(key), "bybit_api_secret": SecretStr(secret)}
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    trading_config = TradingConfig.load(settings.trading_config_path)
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("startup", mode=settings.mode, testnet=settings.bybit_testnet)
        db = make_engine(settings.database_url)
        sm = make_sessionmaker(db)
        ctx = AppContext(
            settings=settings,
            sm=sm,
            repo=TradeRepo(sm, settings.mode.value),
            bus=bus,
            tokens=TokenService(settings.jwt_secret.get_secret_value(), settings.jwt_ttl_minutes),
            secrets=SecretBox(settings.master_key.get_secret_value()),
            config=trading_config,
        )
        app.state.ctx = ctx
        notifier = TelegramNotifier.from_settings(settings, bus, ctx)
        if settings.run_bot and settings.mode is not RunMode.BACKTEST:
            runtime = BotRuntime(await _with_stored_keys(settings, ctx), trading_config, bus, sm)
            await runtime.start()
            ctx.runtime = runtime
            ctx.config = runtime.config
        if notifier is not None:
            await notifier.start()
        try:
            yield
        finally:
            if notifier is not None:
                await notifier.stop()
            if ctx.runtime is not None:
                await ctx.runtime.stop()
            await db.dispose()
            log.info("shutdown")

    app = FastAPI(title="Tradebot", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": settings.mode.value}

    app.include_router(routes.router)
    app.include_router(ws.router)
    _mount_frontend(app, STATIC_DIR)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"
    if not index.exists():
        return
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # SPA: любые не-API пути отдают index.html, маршрутизация — на клиенте
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)
