from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.trading_config import TradingConfig

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    trading_config = TradingConfig.load(settings.trading_config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("startup", mode=settings.mode, testnet=settings.bybit_testnet)
        yield
        log.info("shutdown")

    app = FastAPI(title="Tradebot", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.trading_config = trading_config
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

    return app
