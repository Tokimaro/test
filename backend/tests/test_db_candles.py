from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.candles import SqlCandleStore, dt_to_ms, ms_to_dt
from app.domain import Instrument, MarketType, Timeframe
from tests.fakes import make_candle

pytestmark = pytest.mark.db

H = Timeframe.H1.ms
T0 = 1_700_000_000_000 // H * H


def inst(symbol: str = "BTCUSDT", step: str = "0.001") -> Instrument:
    return Instrument(
        symbol=symbol,
        market_type=MarketType.CRYPTO,
        category="linear",
        tick_size=Decimal("0.1"),
        qty_step=Decimal(step),
        min_qty=Decimal("0.001"),
        max_qty=Decimal(100),
    )


def test_ms_roundtrip_exact() -> None:
    for ms in (0, 1, 1_700_000_000_123, 1_999_999_999_999):
        assert dt_to_ms(ms_to_dt(ms)) == ms


async def test_register_is_idempotent_and_updates(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlCandleStore(db_sessionmaker, broker="bybit")
    a = await store.register(inst())
    b = await store.register(inst(step="0.01"))
    assert a == b


async def test_candles_upsert_range_limit(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlCandleStore(db_sessionmaker, broker="bybit")
    await store.register(inst())
    await store.register(inst("ETHUSDT"))
    assert await store.last_ts("BTCUSDT", Timeframe.H1) is None

    candles = [make_candle(T0 + i * H, close=100 + i) for i in range(5000)]
    await store.save_candles("BTCUSDT", Timeframe.H1, candles)  # > BATCH — несколько пачек
    await store.save_candles("BTCUSDT", Timeframe.H1, [make_candle(T0, close=1.0)])  # upsert
    await store.save_candles("ETHUSDT", Timeframe.H1, [make_candle(T0 + 10**9)])

    assert await store.last_ts("BTCUSDT", Timeframe.H1) == T0 + 4999 * H
    assert await store.last_ts("BTCUSDT", Timeframe.H4) is None

    rng = await store.get_candles("BTCUSDT", Timeframe.H1, T0 + 10 * H, T0 + 12 * H)
    assert [c.ts for c in rng] == [T0 + 10 * H, T0 + 11 * H, T0 + 12 * H]

    last3 = await store.get_candles("BTCUSDT", Timeframe.H1, limit=3)
    assert [c.close for c in last3] == [5097.0, 5098.0, 5099.0]

    first = await store.get_candles("BTCUSDT", Timeframe.H1, end_ms=T0)
    assert first[0].close == 1.0


async def test_unknown_symbol_raises(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    store = SqlCandleStore(db_sessionmaker, broker="bybit")
    with pytest.raises(KeyError):
        await store.last_ts("NOPE", Timeframe.H1)
