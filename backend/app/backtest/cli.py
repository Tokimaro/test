"""Бэктест по истории из БД (сначала загрузите её: python -m app.market.backfill ...).

Пример:
    uv run python -m app.backtest.cli --symbols ETHUSDT SOLUSDT 1000PEPEUSDT
"""

import argparse
import asyncio
import json
from typing import Any

from app.backtest.engine import Backtester, BacktestSettings, SymbolData
from app.backtest.metrics import summarize
from app.config import get_settings
from app.db.candles import SqlCandleStore, load_instrument, ms_to_dt
from app.db.models import BacktestRunRow
from app.db.session import make_engine, make_sessionmaker
from app.domain import Timeframe
from app.market.frame import candles_to_frame
from app.trading_config import TradingConfig


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    config = TradingConfig.load(settings.trading_config_path)
    market = config.markets[args.market]
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    store = SqlCandleStore(sm, broker=None)
    try:
        data = []
        for symbol in args.symbols:
            async with sm() as session:
                inst = await load_instrument(session, None, symbol)
            if inst is None:
                raise SystemExit(f"{symbol}: нет в БД — сначала запустите app.market.backfill")
            daily = candles_to_frame(await store.get_candles(symbol, Timeframe.D1))
            if daily.empty:
                raise SystemExit(f"{symbol}: нет дневных свечей")
            data.append(SymbolData(symbol, inst, daily))

        bt = Backtester(config, market, BacktestSettings(initial_equity=args.equity))
        prepared = bt.prepare(data)
        result = bt.run(prepared)
        report: dict[str, Any] = summarize(result)
        start = int(prepared.closes.index[0])
        end = int(prepared.closes.index[-1])
        async with sm() as session, session.begin():
            session.add(
                BacktestRunRow(
                    params={
                        "symbols": args.symbols,
                        "market": args.market,
                        "config": config.model_dump(mode="json"),
                    },
                    period_start=ms_to_dt(start),
                    period_end=ms_to_dt(end),
                    metrics=report,
                    equity_curve=result.equity_curve[:: max(1, len(result.equity_curve) // 2000)],
                    trades=[t.to_dict() for t in result.trades],
                )
            )
        return report
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--market", default="crypto")
    parser.add_argument("--equity", type=float, default=10_000.0)
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
