import argparse
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest import cli
from app.config import Settings, get_settings
from app.db.candles import SqlCandleStore
from app.db.models import BacktestRunRow
from app.domain import Timeframe
from tests.synthetic import frame_to_candles, make_ohlcv, resample
from tests.test_backtest import INST, T0

pytestmark = pytest.mark.db


async def test_cli_runs_on_db_history(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlCandleStore(db_sessionmaker, broker="bybit")
    await store.register(replace(INST, symbol="BTCUSDT"))
    h1 = make_ohlcv(3000, drift=0.0005, vol=0.008, seed=1, start_ts=T0)
    for tf, df in (
        (Timeframe.H1, h1),
        (Timeframe.H4, resample(h1, Timeframe.H1, Timeframe.H4)),
        (Timeframe.M15, make_ohlcv(12000, vol=0.004, seed=2, tf=Timeframe.M15, start_ts=T0)),
    ):
        await store.save_candles("BTCUSDT", tf, frame_to_candles(df))

    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=migrated_db)
    )
    get_settings.cache_clear()
    args = argparse.Namespace(
        symbols=["BTCUSDT"], market="crypto", equity=10_000.0, walk_forward=True
    )
    report = await cli.main_async(args)
    assert "summary" in report and "walk_forward" in report
    async with db_sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(BacktestRunRow)) == 1


async def test_cli_requires_backfill(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=migrated_db)
    )
    args = argparse.Namespace(symbols=["NOPE"], market="crypto", equity=1.0, walk_forward=False)
    with pytest.raises(SystemExit, match="backfill"):
        await cli.main_async(args)
