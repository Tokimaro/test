"""Сравнение стратегий на длинной истории (дневные бары, 10 монет).

Протокол (зафиксирован до запуска):
* параметры стратегий — из литературы, без подбора по результату;
* in-sample 2018–2021 — только для ознакомления, оценка — out-of-sample 2022-01 → конец данных;
* ML: walk-forward, переобучение каждые 90 дней только на прошлом, прогнозы — вне обучения;
* издержки Bybit (не-VIP) + проскальзывание 0.05% на оборот: перпетуалы — taker 0.055%,
  funding 0.01%/8ч (лонги платят, шорты получают); спот — 0.1%. Стратегии «только лонг» — спот.

    uv run --extra research python -m app.research.run --data ../data/binance
"""

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.dataset import available_symbols, load_candles
from app.domain import Timeframe
from app.research import ml
from app.research import strategies as st
from app.research.portfolio import CostModel, Result, metrics, run_weights, sharpe_ci

DAY = 86_400_000
IS_START = pd.Timestamp("2018-01-01", tz="UTC")
OOS_START = pd.Timestamp("2022-01-01", tz="UTC")
ML_FIRST_TEST = pd.Timestamp("2020-01-01", tz="UTC")  # обучение на 2018–2019


def ms(t: pd.Timestamp) -> int:
    return int(t.timestamp() * 1000)


def evaluate(
    res: Result, is_start: pd.Timestamp = IS_START, oos_start: pd.Timestamp = OOS_START
) -> dict[str, Any]:
    idx = res.returns.index
    is_mask = (idx >= ms(is_start)) & (idx < ms(oos_start))
    oos_mask = idx >= ms(oos_start)
    out: dict[str, Any] = {
        "in_sample": metrics(res.returns[is_mask], res.bars_per_year, res.turnover),
        "out_of_sample": {
            **metrics(res.returns[oos_mask], res.bars_per_year, res.turnover),
            "sharpe_ci95": sharpe_ci(res.returns[oos_mask], res.bars_per_year),
            "gross_sharpe": metrics(res.gross[oos_mask], res.bars_per_year).get("sharpe"),
        },
        "by_year": {},
        **res.meta,
    }
    years = pd.to_datetime(idx, unit="ms", utc=True).year
    for y in sorted(set(years)):
        r = res.returns[years == y]
        out["by_year"][str(y)] = round((float(np.prod(1 + r.to_numpy())) - 1) * 100, 1)
    return out


def run(
    data: Path,
    retrain_days: int,
    symbols: list[str] | None = None,
    is_start: pd.Timestamp = IS_START,
    oos_start: pd.Timestamp = OOS_START,
    ml_first_test: pd.Timestamp = ML_FIRST_TEST,
) -> dict[str, Any]:
    """symbols — торгуемые монеты; BTCUSDT подгружается всегда (рыночный ориентир и фильтр)."""
    tradable = symbols or available_symbols(data, Timeframe.D1)
    loaded = tradable if "BTCUSDT" in tradable else [*tradable, "BTCUSDT"]
    daily = {s: load_candles(data, s, Timeframe.D1) for s in loaded}
    closes = pd.DataFrame({s: d["close"] for s, d in daily.items()}).sort_index()
    trade = closes[tradable]
    bpy = 365.0
    spot = CostModel.spot()
    perp = CostModel()

    def wide(w: pd.DataFrame) -> pd.DataFrame:
        return w.reindex(columns=closes.columns, fill_value=0.0)

    results: list[Result] = [
        run_weights(
            "Бенчмарк: BTC купить и держать",
            st.buy_and_hold(closes, ["BTCUSDT"]),
            closes,
            bpy,
            spot,
        ),
        run_weights(
            f"Бенчмарк: корзина {len(tradable)} монет",
            wide(st.hold_every(st.buy_and_hold(trade), 30)),
            closes,
            bpy,
            spot,
        ),
        run_weights(
            "BTC выше 200-дневной средней (спот)", st.btc_trend_filter(closes), closes, bpy, spot
        ),
        run_weights(
            "Тренд (TSMOM), только лонг, спот",
            wide(st.tsmom(trade, bpy, long_only=True)),
            closes,
            bpy,
            spot,
        ),
        run_weights("Тренд (TSMOM), лонг/шорт", wide(st.tsmom(trade, bpy)), closes, bpy, perp),
        run_weights(
            "Кросс-секционный моментум 4 недели",
            wide(st.xs_momentum(trade, bpy, vol_managed=False)),
            closes,
            bpy,
            perp,
        ),
        run_weights(
            "Кросс-секционный моментум + упр. волатильностью",
            wide(st.xs_momentum(trade, bpy)),
            closes,
            bpy,
            perp,
        ),
        run_weights(
            "Краткосрочный разворот (1 день)", wide(st.st_reversal(trade)), closes, bpy, perp
        ),
    ]

    panel = ml.build_panel(daily)
    ml_quality: dict[str, Any] = {}
    for name, model in (
        ("Ridge (линейная)", ml.ridge_model),
        ("LightGBM", ml.lgbm_model),
        ("Нейросеть MLP", ml.mlp_model),
    ):
        pred = ml.walk_forward_predict(panel, model, name, ms(ml_first_test), retrain_days)
        oos = ml.Prediction(name, pred.frame[pred.frame["ts"] >= ms(oos_start)])
        ml_quality[name] = ml.information_coefficient(oos)
        for smooth, suffix in ((1, "ежедневно"), (5, "сглаженный прогноз")):
            w = wide(ml.prediction_weights(pred, closes.index, trade.columns, smooth=smooth))
            res = run_weights(f"ML {name}: лонг/шорт, {suffix}", w, closes, bpy, perp)
            res.meta["ml_quality_oos"] = ml_quality[name]
            results.append(res)

    return {
        "symbols": tradable,
        "period": [
            str(pd.to_datetime(closes.index[0], unit="ms").date()),
            str(pd.to_datetime(closes.index[-1], unit="ms").date()),
        ],
        "in_sample_period": [str(is_start.date()), str(oos_start.date())],
        "strategies": {r.name: evaluate(r, is_start, oos_start) for r in results},
        "ml_quality_oos": ml_quality,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--retrain-days", type=int, default=90)
    p.add_argument("--out", help="сохранить отчёт в JSON")
    p.add_argument("--symbols", nargs="*", help="монеты (по умолчанию — все в датасете)")
    p.add_argument("--is-start", default=str(IS_START.date()))
    p.add_argument("--oos-start", default=str(OOS_START.date()))
    p.add_argument("--ml-first-test", default=str(ML_FIRST_TEST.date()))
    args = p.parse_args()
    report = run(
        Path(args.data),
        args.retrain_days,
        args.symbols,
        pd.Timestamp(args.is_start, tz="UTC"),
        pd.Timestamp(args.oos_start, tz="UTC"),
        pd.Timestamp(args.ml_first_test, tz="UTC"),
    )
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
