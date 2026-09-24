"""Углублённая проверка лучшего кандидата — SMC Fair Value Gap.

Кандидат выбран после основного эксперимента (intraday_run): это единственная стратегия,
у которой чистый результат положителен и в отборе, и вне выборки, и на новых монетах. Раз выбор
сделан с оглядкой на проверочный период, здесь проверяется, не держится ли результат на одной
удачной настройке:
* соседние таймфреймы (15m…4h), тейк 2R/3R, с фильтром тренда и без;
* строгое исполнение лимиток: цена должна пройти за лимит на 0.05% (очередь в стакане);
* повышенные издержки (taker 0.12%, maker 0.04% на сторону);
* разбивка по монетам, годам, направлению сделок.

    cd backend && uv run --extra research python -m app.research.intraday_fvg --data ../data/binance
"""

import argparse
import itertools
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest.dataset import load_candles
from app.domain import Timeframe
from app.research import intraday as it
from app.research.intraday_run import HOLDOUT_SYMBOLS, SELECT, SELECT_SYMBOLS, TEST, period

TIMEFRAMES = ["15m", "30m", "1h", "2h", "4h"]
STRESS = it.Costs(taker=0.0012, maker=0.0004)
MAIN = "1h|rr=3.0|bias=1|fill=0"


def run_symbol(args: tuple[Path, str]) -> pd.DataFrame:
    data, symbol = args
    base = it.to_frame(load_candles(data, symbol, Timeframe.M1))
    out = []
    for tf in TIMEFRAMES:
        df = it.resample(base, tf)
        for rr, bias in itertools.product((2.0, 3.0), (False, True)):
            sig = it.fvg_retest(df, rr=rr, use_bias=bias)
            for fill in (0.0, 0.0005):
                t = it.simulate(df, sig, fill_through=fill)
                t["symbol"] = symbol
                t["config"] = f"{tf}|rr={rr}|bias={int(bias)}|fill={fill * 100:g}"
                out.append(t)
    res = pd.concat([t for t in out if not t.empty], ignore_index=True)
    for col in ("signal_ts", "exit_ts"):
        res[col] = pd.to_datetime(res[col], utc=True)
    return res


def blocks(trades: pd.DataFrame, costs: it.Costs) -> dict[str, Any]:
    same = trades[trades["symbol"].isin(SELECT_SYMBOLS)]
    new = trades[trades["symbol"].isin(HOLDOUT_SYMBOLS)]
    return {
        "select_2023_2024": it.summarize(period(same, SELECT), costs),
        "test_same_coins": it.summarize(period(same, TEST), costs),
        "test_new_coins": it.summarize(period(new, TEST), costs),
        "all_coins_2023_2026": it.summarize(trades, costs),
    }


def run(data: Path) -> dict[str, Any]:
    symbols = SELECT_SYMBOLS + HOLDOUT_SYMBOLS
    with ProcessPoolExecutor(max_workers=3) as pool:
        trades = pd.concat(pool.map(run_symbol, [(data, s) for s in symbols]), ignore_index=True)

    grid = {
        str(cfg): {
            "mixed": blocks(g, it.MIXED),
            "stress": blocks(g, STRESS),
            "gross": blocks(g, it.FREE),
        }
        for cfg, g in trades.groupby("config")
    }
    main = trades[trades["config"] == MAIN]
    test = period(main, TEST)
    return {
        "grid": grid,
        "main": {
            "config": MAIN,
            "by_symbol_test": {
                str(s): it.summarize(g, it.MIXED) for s, g in test.groupby("symbol")
            },
            "by_year_all_coins": {
                str(y): it.summarize(g, it.MIXED)
                for y, g in main.groupby(main["signal_ts"].dt.year)
            },
            "by_direction_test": {
                "long": it.summarize(test[test["dir"] > 0], it.MIXED),
                "short": it.summarize(test[test["dir"] < 0], it.MIXED),
            },
            "exits_test": test["reason"].value_counts().to_dict(),
            "median_hold_hours_test": float(
                ((test["exit_ts"] - test["signal_ts"]).dt.total_seconds() / 3600).median()
            ),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--out", help="сохранить отчёт в JSON")
    args = p.parse_args()
    report = run(Path(args.data))
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(json.dumps(report["main"], ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
