import argparse
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest import cli
from app.config import Settings, get_settings
from app.db.candles import SqlCandleStore
from app.db.models import BacktestRunRow
from app.domain import Instrument, MarketType, Timeframe
from tests.synthetic import frame_to_candles, make_ohlcv

pytestmark = pytest.mark.db


async def test_cli_runs_on_db_history(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlCandleStore(db_sessionmaker, broker="bybit")
    for i, symbol in enumerate(("BTCUSDT", "ETHUSDT")):
        await store.register(
            Instrument(
                symbol,
                MarketType.CRYPTO,
                "spot",
                Decimal("0.01"),
                Decimal("0.00001"),
                Decimal("0.00001"),
                Decimal(10**6),
                taker_fee=Decimal("0.001"),
                maker_fee=Decimal("0.001"),
            )
        )
        daily = make_ohlcv(600, drift=0.002, vol=0.03, seed=i + 1, tf=Timeframe.D1)
        await store.save_candles(symbol, Timeframe.D1, frame_to_candles(daily))

    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, database_url=migrated_db)
    )
    get_settings.cache_clear()
    args = argparse.Namespace(symbols=["BTCUSDT", "ETHUSDT"], market="crypto", equity=10_000.0)
    report = await cli.main_async(args)
    assert report["summary"]["trades"] > 0 and "sharpe" in report["summary"]
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
    args = argparse.Namespace(symbols=["NOPE"], market="crypto", equity=1.0)
    with pytest.raises(SystemExit, match="backfill"):
        await cli.main_async(args)
