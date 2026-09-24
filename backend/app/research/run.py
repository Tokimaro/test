"""Сравнение стратегий на длинной истории (дневные бары, 10 монет).

Протокол (зафиксирован до запуска):
* параметры стратегий — из литературы, без подбора по результату;
* in-sample 2018–2021 — только для ознакомления, оценка — out-of-sample 2022-01 → конец данных;
* ML: walk-forward, переобучение каждые 90 дней только на прошлом, прогнозы — вне обучения;
* издержки: комиссия taker 0.055% + проскальзывание 0.05% на оборот, funding 0.01%/8ч
  для перпетуалов (лонги платят, шорты получают); стратегии «только лонг» — спот.

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


def evaluate(res: Result) -> dict[str, Any]:
    idx = res.returns.index
    is_mask = (idx >= ms(IS_START)) & (idx < ms(OOS_START))
    oos_mask = idx >= ms(OOS_START)
    out: dict[str, Any] = {
        "in_sample_2018_2021": metrics(res.returns[is_mask], res.bars_per_year, res.turnover),
        "out_of_sample_2022_2026": {
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


def run(data: Path, retrain_days: int) -> dict[str, Any]:
    symbols = available_symbols(data, Timeframe.D1)
    daily = {s: load_candles(data, s, Timeframe.D1) for s in symbols}
    closes = pd.DataFrame({s: d["close"] for s, d in daily.items()}).sort_index()
    bpy = 365.0
    spot = CostModel(perpetual=False)
    perp = CostModel()

    results: list[Result] = [
        run_weights(
            "Бенчмарк: BTC купить и держать",
            st.buy_and_hold(closes, ["BTCUSDT"]),
            closes,
            bpy,
            spot,
        ),
        run_weights(
            "Бенчмарк: корзина 10 монет",
            st.hold_every(st.buy_and_hold(closes), 30),
            closes,
            bpy,
            spot,
        ),
        run_weights(
            "BTC выше 200-дневной средней (спот)", st.btc_trend_filter(closes), closes, bpy, spot
        ),
        run_weights(
            "Тренд (TSMOM), только лонг, спот",
            st.tsmom(closes, bpy, long_only=True),
            closes,
            bpy,
            spot,
        ),
        run_weights("Тренд (TSMOM), лонг/шорт", st.tsmom(closes, bpy), closes, bpy, perp),
        run_weights(
            "Кросс-секционный моментум 4 недели",
            st.xs_momentum(closes, bpy, vol_managed=False),
            closes,
            bpy,
            perp,
        ),
        run_weights(
            "Кросс-секционный моментум + упр. волатильностью",
            st.xs_momentum(closes, bpy),
            closes,
            bpy,
            perp,
        ),
        run_weights("Краткосрочный разворот (1 день)", st.st_reversal(closes), closes, bpy, perp),
    ]

    panel = ml.build_panel(daily)
    ml_quality: dict[str, Any] = {}
    for name, model in (
        ("Ridge (линейная)", ml.ridge_model),
        ("LightGBM", ml.lgbm_model),
        ("Нейросеть MLP", ml.mlp_model),
    ):
        pred = ml.walk_forward_predict(panel, model, name, ms(ML_FIRST_TEST), retrain_days)
        oos = ml.Prediction(name, pred.frame[pred.frame["ts"] >= ms(OOS_START)])
        ml_quality[name] = ml.information_coefficient(oos)
        for smooth, suffix in ((1, "ежедневно"), (5, "сглаженный прогноз")):
            w = ml.prediction_weights(pred, closes.index, closes.columns, smooth=smooth)
            res = run_weights(f"ML {name}: лонг/шорт, {suffix}", w, closes, bpy, perp)
            res.meta["ml_quality_oos"] = ml_quality[name]
            results.append(res)

    return {
        "symbols": symbols,
        "period": [
            str(pd.to_datetime(closes.index[0], unit="ms").date()),
            str(pd.to_datetime(closes.index[-1], unit="ms").date()),
        ],
        "strategies": {r.name: evaluate(r) for r in results},
        "ml_quality_oos": ml_quality,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--retrain-days", type=int, default=90)
    p.add_argument("--out", help="сохранить отчёт в JSON")
    args = p.parse_args()
    report = run(Path(args.data), args.retrain_days)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
