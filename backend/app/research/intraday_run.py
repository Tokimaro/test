"""Сравнение внутридневных стратегий (скальпинг, SMC, внутридневной тренд) на 1m-данных.

Протокол (зафиксирован до запуска):
* отбор: 2023-01 → 2024-12 на BTC, ETH, SOL — для каждой стратегии перебор небольшой сетки
  (таймфрейм 1m/5m/15m × отношение тейк/стоп × фильтр часового тренда);
* лучшая конфигурация каждой стратегии и лучшая стратегия в целом выбираются ТОЛЬКО по
  отбору — по среднему чистому R при реалистичных издержках (лимитный вход и тейк — maker,
  рыночный вход, стоп и выход по времени — taker), не менее 100 сделок;
* проверка: 2025-01 → конец данных на тех же монетах и на XRP, DOGE, BNB, ADA,
  которые в отборе не участвовали;
* все сделки закрываются только по стопу или тейку (кроме стратегий с выходом по времени).

    cd backend && uv run --extra research python -m app.research.intraday_run --data ../data/binance
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

SELECT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HOLDOUT_SYMBOLS = ["XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT"]
SELECT = (pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC"))
TEST = (pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2100-01-01", tz="UTC"))
TIMEFRAMES = ["1m", "5m", "15m", "1h"]  # 1h — ориентир: как размер стопа меняет вес издержек
MIN_TRADES = 100

TIME_BASED = {"seasonality_21_23", "intraday_momentum"}


def grid(name: str) -> list[dict[str, Any]]:
    if name in TIME_BASED:
        return [{"tf": "1m", "rr": 0.0, "use_bias": b} for b in (False, True)]
    rrs = (1.0, 1.5, 2.0) if name == "bb_rsi_reversion" else (1.5, 2.0, 3.0)
    return [
        {"tf": tf, "rr": rr, "use_bias": b}
        for tf, rr, b in itertools.product(TIMEFRAMES, rrs, (False, True))
    ]


def config_key(name: str, cfg: dict[str, Any]) -> str:
    return f"{name}|{cfg['tf']}|rr={cfg['rr']}|bias={int(cfg['use_bias'])}"


def run_symbol(args: tuple[Path, str]) -> pd.DataFrame:
    data, symbol = args
    base = it.to_frame(load_candles(data, symbol, Timeframe.M1))
    frames = {tf: it.resample(base, tf) for tf in TIMEFRAMES}
    out = []
    for name, fn in it.STRATEGIES.items():
        for cfg in grid(name):
            df = frames[cfg["tf"]]
            trades = it.simulate(df, fn(df, rr=cfg["rr"], use_bias=cfg["use_bias"]))
            trades = trades.drop(columns=["sl", "bars"])
            trades["symbol"] = symbol
            trades["config"] = config_key(name, cfg)
            trades["strategy"] = name
            if not trades.empty:
                out.append(trades)
    res = pd.concat(out, ignore_index=True)
    for col in ("signal_ts", "exit_ts"):
        res[col] = pd.to_datetime(res[col], utc=True)
    for col in ("symbol", "config", "strategy", "reason"):  # экономия памяти
        res[col] = res[col].astype("category")
    return res


def period(trades: pd.DataFrame, span: tuple[pd.Timestamp, pd.Timestamp]) -> pd.DataFrame:
    return trades[(trades["signal_ts"] >= span[0]) & (trades["signal_ts"] < span[1])]


def simulate_all(data: Path, cache: Path | None = None) -> pd.DataFrame:
    """Сделки всех конфигураций по всем монетам; с cache — сохраняются в Parquet."""
    if cache and cache.exists():
        return pd.read_parquet(cache)
    symbols = SELECT_SYMBOLS + HOLDOUT_SYMBOLS
    with ProcessPoolExecutor(max_workers=3) as pool:
        parts = list(pool.map(run_symbol, [(data, s) for s in symbols]))
    all_trades = pd.concat(parts, ignore_index=True)
    del parts
    if cache:
        all_trades.to_parquet(cache)
    return all_trades


def run(data: Path, cache: Path | None = None) -> dict[str, Any]:
    all_trades = simulate_all(data, cache)
    for col in ("symbol", "config", "strategy", "reason"):
        all_trades[col] = all_trades[col].astype("category")
    sel = period(all_trades[all_trades["symbol"].isin(SELECT_SYMBOLS)], SELECT)

    # 1) отбор: лучшая конфигурация каждой стратегии
    per_config = {
        str(key): {
            "taker": it.summarize(g, it.TAKER),
            "mixed": it.summarize(g, it.MIXED),
            "gross": it.summarize(g, it.FREE),
        }
        for key, g in sel.groupby("config")
    }
    best_per_strategy: dict[str, str] = {}
    for key, m in per_config.items():
        name = key.split("|")[0]
        if m["mixed"]["trades"] < MIN_TRADES:
            continue
        cur = best_per_strategy.get(name)
        if cur is None or m["mixed"]["avg_r"] > per_config[cur]["mixed"]["avg_r"]:
            best_per_strategy[name] = key

    test_all = period(all_trades, TEST)
    per_config_test = {
        str(key): {"mixed": it.summarize(g, it.MIXED), "gross": it.summarize(g, it.FREE)}
        for key, g in test_all.groupby("config")
    }

    # 2) проверка вне выборки: лучшая конфигурация каждой стратегии
    test = period(all_trades, TEST)
    report: dict[str, Any] = {}
    for name, key in sorted(
        best_per_strategy.items(), key=lambda kv: -per_config[kv[1]]["mixed"]["avg_r"]
    ):
        g_test = test[test["config"] == key]
        report[name] = {
            "config": key,
            "select_2023_2024": per_config[key],
            "test_2025_2026_same_coins": {
                "taker": it.summarize(g_test[g_test["symbol"].isin(SELECT_SYMBOLS)], it.TAKER),
                "mixed": it.summarize(g_test[g_test["symbol"].isin(SELECT_SYMBOLS)], it.MIXED),
                "gross": it.summarize(g_test[g_test["symbol"].isin(SELECT_SYMBOLS)], it.FREE),
            },
            "test_2025_2026_new_coins": {
                "taker": it.summarize(g_test[g_test["symbol"].isin(HOLDOUT_SYMBOLS)], it.TAKER),
                "mixed": it.summarize(g_test[g_test["symbol"].isin(HOLDOUT_SYMBOLS)], it.MIXED),
                "gross": it.summarize(g_test[g_test["symbol"].isin(HOLDOUT_SYMBOLS)], it.FREE),
            },
        }

    # 3) финальная проверка лучшей стратегии: по монетам, по годам, соседние параметры
    winner_name = next(iter(report)) if report else None
    final: dict[str, Any] = {}
    if winner_name:
        key = report[winner_name]["config"]
        g = all_trades[all_trades["config"] == key]
        final["config"] = key
        final["by_symbol_test"] = {
            s: it.summarize(period(gs, TEST), it.MIXED) for s, gs in g.groupby("symbol")
        }
        years = g["signal_ts"].dt.year
        final["by_year_all_coins"] = {
            str(y): it.summarize(gy, it.MIXED) for y, gy in g.groupby(years)
        }
        final["neighbours_select"] = {
            k: m["mixed"] for k, m in per_config.items() if k.startswith(winner_name + "|")
        }
        final["neighbours_test"] = {
            str(k): it.summarize(period(gk, TEST), it.MIXED)
            for k, gk in all_trades[all_trades["strategy"] == winner_name].groupby("config")
        }
        final["exits"] = g["reason"].value_counts().to_dict()

    return {
        "select_symbols": SELECT_SYMBOLS,
        "holdout_symbols": HOLDOUT_SYMBOLS,
        "costs_per_side_pct": {"taker": it.TAKER.taker * 100, "maker": it.MIXED.maker * 100},
        "all_configs_select": per_config,
        "all_configs_test_all_coins": per_config_test,
        "best_per_strategy": report,
        "winner": winner_name,
        "final_check": final,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--out", help="сохранить отчёт в JSON")
    p.add_argument("--cache", help="Parquet-файл для кэша сделок (пересчёт анализа без симуляции)")
    args = p.parse_args()
    report = run(Path(args.data), Path(args.cache) if args.cache else None)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(json.dumps(report["best_per_strategy"], ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
