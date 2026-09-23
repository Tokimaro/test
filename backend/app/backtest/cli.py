"""Бэктест по истории из БД (сначала загрузите её: python -m app.market.backfill ...).

Пример:
    uv run python -m app.backtest.cli --symbols BTCUSDT ETHUSDT SOLUSDT --walk-forward
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from app.analysis.indicators import candles_to_frame
from app.backtest.engine import Backtester, BacktestSettings, SymbolData
from app.backtest.metrics import monte_carlo_drawdown, summarize
from app.backtest.walk_forward import walk_forward
from app.config import get_settings
from app.db.candles import SqlCandleStore, load_instrument, ms_to_dt
from app.db.models import BacktestRunRow
from app.db.session import make_engine, make_sessionmaker
from app.trading_config import TradingConfig


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    config = TradingConfig.load(settings.trading_config_path)
    market = config.markets[args.market]
    tfs = market.timeframes
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    store = SqlCandleStore(sm, broker="bybit")
    try:
        data = []
        for symbol in args.symbols:
            async with sm() as session:
                inst = await load_instrument(session, "bybit", symbol)
            if inst is None:
                raise SystemExit(f"{symbol}: нет в БД — сначала запустите app.market.backfill")
            frames = {}
            for tf in (tfs.working, tfs.higher, tfs.entry):
                candles = await store.get_candles(symbol, tf)
                frames[tf] = candles_to_frame(candles)
            if frames[tfs.working].empty or frames[tfs.higher].empty:
                raise SystemExit(f"{symbol}: нет свечей рабочего/старшего таймфрейма")
            data.append(
                SymbolData(
                    symbol,
                    inst,
                    frames[tfs.working],
                    frames[tfs.higher],
                    frames[tfs.entry] if not frames[tfs.entry].empty else None,
                )
            )

        bt = Backtester(config, market, BacktestSettings(initial_equity=args.equity))
        prepared = bt.prepare(data)
        result = bt.run(prepared)
        report: dict[str, Any] = summarize(result)
        report["monte_carlo"] = monte_carlo_drawdown(
            [t.r_multiple for t in result.trades], config.risk.risk_per_trade_pct
        )
        if args.walk_forward:
            wf = walk_forward(bt, prepared)
            report["walk_forward"] = {
                "summary": wf.summary,
                "windows": [
                    {
                        "oos_start": datetime.fromtimestamp(w.oos_start / 1000, UTC)
                        .date()
                        .isoformat(),
                        "threshold": w.best_threshold,
                        **w.oos_stats,
                    }
                    for w in wf.windows
                ],
            }

        start = min(int(p.index[0]) for p in prepared)
        end = max(int(p.index[-1]) for p in prepared)
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
    parser.add_argument("--walk-forward", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
