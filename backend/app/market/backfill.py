"""Загрузка истории свечей в БД.

Пример:
    uv run python -m app.market.backfill --symbols BTCUSDT ETHUSDT --tf 60 240 --days 730
"""

import argparse
import asyncio

import structlog

from app.brokers.bybit.adapter import BybitAdapter
from app.brokers.bybit.client import BybitHttpClient
from app.config import get_settings
from app.db.candles import SqlCandleStore
from app.db.session import make_engine, make_sessionmaker
from app.domain import Timeframe
from app.logging import configure_logging
from app.market.feed import wall_clock_ms

log = structlog.get_logger()


async def backfill(symbols: list[str], tfs: list[Timeframe], days: int, category: str) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    store = SqlCandleStore(make_sessionmaker(engine), broker="bybit")
    client = BybitHttpClient(testnet=False)  # история с mainnet полнее, ключи не нужны
    adapter = BybitAdapter(client, category=category, testnet=False)
    try:
        now = wall_clock_ms()
        for symbol in symbols:
            await store.register(await adapter.get_instrument(symbol))
            for tf in tfs:
                last = await store.last_ts(symbol, tf)
                start = last + tf.ms if last is not None else now - days * 86_400_000
                start = start // tf.ms * tf.ms
                candles = await adapter.get_candles(symbol, tf, start, now)
                closed = [c for c in candles if c.ts + tf.ms <= now]
                await store.save_candles(symbol, tf, closed)
                log.info("backfill.done", symbol=symbol, tf=tf.value, bars=len(closed))
    finally:
        await adapter.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--tf", nargs="+", default=["15", "60", "240"])
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--category", default="linear")
    args = parser.parse_args()
    configure_logging(json=False)
    asyncio.run(backfill(args.symbols, [Timeframe(t) for t in args.tf], args.days, args.category))


if __name__ == "__main__":
    main()
