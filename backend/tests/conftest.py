import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from app.config import BACKEND_DIR, RunMode, Settings
from app.db.session import make_engine, make_sessionmaker

TEST_DB_URL = os.environ.get("TB_TEST_DATABASE_URL")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, mode=RunMode.PAPER, log_json=False)


# ---------------------------------------------------------------- БД (опционально)
def _alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["url"] = url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[str]:
    if not TEST_DB_URL:
        pytest.skip("TB_TEST_DATABASE_URL не задан")
    cfg = _alembic_config(TEST_DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield TEST_DB_URL
    command.downgrade(cfg, "base")


ALL_TABLES = (
    "candles, executions, orders, trades, signals, instruments, equity_snapshots, "
    "settings, risk_events, backtest_runs, users, secrets"
)


@pytest.fixture
async def db_sessionmaker(migrated_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
    yield make_sessionmaker(engine)
    await engine.dispose()
