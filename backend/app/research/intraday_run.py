"""Сравнение внутридневных стратегий (скальпинг, SMC, внутридневной тренд) на 1m-данных.

Протокол (зафиксирован до запуска):
* отбор: 2023-01 → 2024-12 на монетах отбора — для каждой стратегии перебор небольшой сетки
  (таймфрейм 1m/5m/15m/1h × отношение тейк/стоп × фильтр часового тренда);
* лучшая конфигурация каждой стратегии и лучшая стратегия в целом выбираются ТОЛЬКО по
  отбору — по среднему чистому R при реалистичных издержках (лимитный вход и тейк — maker,
  рыночный вход, стоп и выход по времени — taker), не менее 100 сделок;
* проверка: 2025-01 → конец данных на тех же монетах и на монетах, не участвовавших в отборе;
* наборы монет (--universe): top — BTC/ETH/SOL + XRP/DOGE/BNB/ADA (спот Binance);
  volatile — 12 волатильных альткоинов (перпетуалы Binance USDT-M, см. docs/volatile-pairs.md);
* все сделки закрываются только по стопу или тейку (кроме стратегий с выходом по времени).

    cd backend && uv run --extra research python -m app.research.intraday_run --data ../data/binance
    cd backend && uv run --extra research python -m app.research.intraday_run \
        --data ../data/binance_um --universe volatile --slippage 0.0005
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

UNIVERSES = {
    "top": (["BTCUSDT", "ETHUSDT", "SOLUSDT"], ["XRPUSDT", "DOGEUSDT", "BNBUSDT", "ADAUSDT"]),
    # волатильность 2025–26 ≥ 100% годовых, медианный оборот ≥ $25 млн/день, история с 2024-02;
    # 12 монет по убыванию волатильности попеременно в отбор и в проверку
    "volatile": (
        ["ORDIUSDT", "ENAUSDT", "1000BONKUSDT", "TIAUSDT", "1000PEPEUSDT", "PENDLEUSDT"],
        ["WLDUSDT", "WIFUSDT", "FETUSDT", "DYDXUSDT", "LDOUSDT", "JUPUSDT"],
    ),
}
SELECT_SYMBOLS, HOLDOUT_SYMBOLS = UNIVERSES["top"]
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


def simulate_all(data: Path, symbols: list[str], cache: Path | None = None) -> pd.DataFrame:
    """Сделки всех конфигураций по всем монетам; с cache — сохраняются в Parquet."""
    if cache and cache.exists():
        return pd.read_parquet(cache)
    with ProcessPoolExecutor(max_workers=3) as pool:
        parts = list(pool.map(run_symbol, [(data, s) for s in symbols]))
    all_trades = pd.concat(parts, ignore_index=True)
    del parts
    if cache:
        all_trades.to_parquet(cache)
    return all_trades


def run(
    data: Path,
    cache: Path | None = None,
    universe: str = "top",
    slippage: float = it.SLIPPAGE,
) -> dict[str, Any]:
    select_symbols, holdout_symbols = UNIVERSES[universe]
    costs = it.cost_models(slippage)
    taker, mixed = costs["taker"], costs["mixed"]
    all_trades = simulate_all(data, select_symbols + holdout_symbols, cache)
    for col in ("symbol", "config", "strategy", "reason"):
        all_trades[col] = all_trades[col].astype("category")
    sel = period(all_trades[all_trades["symbol"].isin(select_symbols)], SELECT)

    # 1) отбор: лучшая конфигурация каждой стратегии
    per_config = {
        str(key): {
            "taker": it.summarize(g, taker),
            "mixed": it.summarize(g, mixed),
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
        str(key): {"mixed": it.summarize(g, mixed), "gross": it.summarize(g, it.FREE)}
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
                "taker": it.summarize(g_test[g_test["symbol"].isin(select_symbols)], taker),
                "mixed": it.summarize(g_test[g_test["symbol"].isin(select_symbols)], mixed),
                "gross": it.summarize(g_test[g_test["symbol"].isin(select_symbols)], it.FREE),
            },
            "test_2025_2026_new_coins": {
                "taker": it.summarize(g_test[g_test["symbol"].isin(holdout_symbols)], taker),
                "mixed": it.summarize(g_test[g_test["symbol"].isin(holdout_symbols)], mixed),
                "gross": it.summarize(g_test[g_test["symbol"].isin(holdout_symbols)], it.FREE),
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
            s: it.summarize(period(gs, TEST), mixed) for s, gs in g.groupby("symbol")
        }
        years = g["signal_ts"].dt.year
        final["by_year_all_coins"] = {str(y): it.summarize(gy, mixed) for y, gy in g.groupby(years)}
        final["neighbours_select"] = {
            k: m["mixed"] for k, m in per_config.items() if k.startswith(winner_name + "|")
        }
        final["neighbours_test"] = {
            str(k): it.summarize(period(gk, TEST), mixed)
            for k, gk in all_trades[all_trades["strategy"] == winner_name].groupby("config")
        }
        final["exits"] = g["reason"].value_counts().to_dict()

    return {
        "select_symbols": select_symbols,
        "holdout_symbols": holdout_symbols,
        "universe": universe,
        "costs_per_side_pct": {"taker": taker.taker * 100, "maker": mixed.maker * 100},
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
    p.add_argument("--universe", choices=sorted(UNIVERSES), default="top")
    p.add_argument("--slippage", type=float, default=it.SLIPPAGE, help="доля цены на сторону")
    args = p.parse_args()
    report = run(
        Path(args.data),
        Path(args.cache) if args.cache else None,
        args.universe,
        args.slippage,
    )
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(json.dumps(report["best_per_strategy"], ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
