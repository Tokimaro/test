"""Загрузка истории свечей в БД.

Пример (стратегии нужны дневные свечи спота, 120+ дней; для бэктеста — несколько лет):
    uv run python -m app.market.backfill --symbols BTCUSDT ETHUSDT SOLUSDT --days 1500

Для Bybit история берётся с mainnet (ключи не нужны), для Alpaca нужны TB_ALPACA_API_KEY/SECRET.
"""

import argparse
import asyncio

import structlog

from app.brokers.alpaca import DATA_URL, PAPER_URL, AlpacaAdapter, AlpacaClient
from app.brokers.base import BrokerAdapter
from app.brokers.bybit.adapter import BybitAdapter
from app.brokers.bybit.client import BybitHttpClient
from app.config import get_settings
from app.db.candles import SqlCandleStore
from app.db.session import make_engine, make_sessionmaker
from app.domain import Timeframe
from app.logging import configure_logging
from app.market.feed import wall_clock_ms

log = structlog.get_logger()


def make_adapter(broker: str, category: str) -> BrokerAdapter:
    if broker == "alpaca":
        s = get_settings()
        key, secret = s.alpaca_api_key.get_secret_value(), s.alpaca_api_secret.get_secret_value()
        if not (key and secret):
            raise SystemExit("для Alpaca задайте TB_ALPACA_API_KEY и TB_ALPACA_API_SECRET")
        return AlpacaAdapter(
            AlpacaClient(PAPER_URL, key, secret),
            AlpacaClient(DATA_URL, key, secret),
            feed=s.alpaca_feed,
        )
    return BybitAdapter(BybitHttpClient(testnet=False), category=category, testnet=False)


async def backfill(
    symbols: list[str], tfs: list[Timeframe], days: int, broker: str, category: str
) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    store = SqlCandleStore(make_sessionmaker(engine), broker=broker)
    adapter = make_adapter(broker, category)
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
    parser.add_argument("--tf", nargs="+", default=["D"])
    parser.add_argument("--days", type=int, default=1500)
    parser.add_argument("--broker", choices=["bybit", "alpaca"], default="bybit")
    parser.add_argument("--category", default="spot")
    args = parser.parse_args()
    configure_logging(json=False)
    asyncio.run(
        backfill(
            args.symbols, [Timeframe(t) for t in args.tf], args.days, args.broker, args.category
        )
    )


if __name__ == "__main__":
    main()
