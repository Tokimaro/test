"""ML-прогноз доходности (подход победителей Kaggle G-Research Crypto Forecasting:
признаки + градиентный бустинг) и нейросеть для сравнения.

Задача: по данным на закрытии дня t предсказать доходность монеты за день t+1
относительно среднего по рынку (кросс-секционная задача, как в G-Research).
Обучение — walk-forward: модель переобучается каждые retrain_days на ВСЕЙ истории
до даты переобучения и прогнозирует только будущие дни, которых не видела.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

FEATURES = [
    "r1",
    "r3",
    "r7",
    "r14",
    "r30",
    "vol7",
    "vol30",
    "vol_ratio",
    "rsi14",
    "ma20",
    "ma50",
    "ma200",
    "volume_z",
    "range",
    "btc_r1",
    "btc_r7",
    "btc_r30",
    "rank_r7",
    "rank_r30",
    "rank_vol30",
]


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def build_panel(daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Панель (дата, монета) с признаками на закрытии дня t и целью — доходностью t+1."""
    closes = pd.DataFrame({s: d["close"] for s, d in daily.items()}).sort_index()
    btc = closes["BTCUSDT"]
    frames = []
    for s, d in daily.items():
        c = d["close"]
        r = c.pct_change()
        f = pd.DataFrame(index=d.index)
        for k in (1, 3, 7, 14, 30):
            f[f"r{k}"] = np.log(c / c.shift(k))
        f["vol7"] = r.rolling(7).std()
        f["vol30"] = r.rolling(30).std()
        f["vol_ratio"] = f["vol7"] / f["vol30"]
        f["rsi14"] = _rsi(c)
        for m in (20, 50, 200):
            f[f"ma{m}"] = c / c.rolling(m).mean() - 1
        lv = np.log(d["volume"].replace(0, np.nan))
        f["volume_z"] = (lv - lv.rolling(30).mean()) / lv.rolling(30).std()
        f["range"] = (d["high"] - d["low"]) / c
        b = btc.reindex(d.index)
        f["btc_r1"] = np.log(b / b.shift(1))
        f["btc_r7"] = np.log(b / b.shift(7))
        f["btc_r30"] = np.log(b / b.shift(30))
        f["fwd"] = c.shift(-1) / c - 1  # цель: доходность следующего дня (только для обучения)
        f["symbol"] = s
        frames.append(f)
    panel = pd.concat(frames).rename_axis("ts").reset_index()
    for col, name in (("r7", "rank_r7"), ("r30", "rank_r30"), ("vol30", "rank_vol30")):
        panel[name] = panel.groupby("ts")[col].rank(pct=True)
    # кросс-секционная цель: доходность относительно среднего по монетам в этот день
    panel["target"] = panel["fwd"] - panel.groupby("ts")["fwd"].transform("mean")
    panel = panel.replace([np.inf, -np.inf], np.nan)
    return panel.dropna(subset=FEATURES).reset_index(drop=True)


Model = Callable[[], Any]


def lgbm_model() -> Any:
    import lightgbm as lgb

    # консервативные параметры заданы заранее: неглубокие деревья, сильная регуляризация
    return lgb.LGBMRegressor(
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=200,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_lambda=5.0,
        verbose=-1,
        random_state=0,
    )


def mlp_model() -> Any:
    from sklearn.neural_network import MLPRegressor
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(
        StandardScaler(),
        MLPRegressor(
            hidden_layer_sizes=(32, 16),
            alpha=1e-3,
            learning_rate_init=1e-3,
            early_stopping=True,
            max_iter=300,
            random_state=0,
        ),
    )


def ridge_model() -> Any:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(StandardScaler(), Ridge(alpha=10.0))


@dataclass
class Prediction:
    name: str
    frame: pd.DataFrame  # ts, symbol, pred, target, fwd — только вне обучения


def walk_forward_predict(
    panel: pd.DataFrame,
    make_model: Model,
    name: str,
    first_test: pd.Timestamp | int,
    retrain_days: int = 90,
) -> Prediction:
    day = 86_400_000
    ts = panel["ts"].to_numpy()
    y = panel["target"].clip(-0.2, 0.2)  # выбросы не должны управлять обучением
    start = int(first_test)
    end = int(ts.max())
    out = []
    while start <= end:
        stop = start + retrain_days * day
        # строка дня t содержит цель, известную на закрытии t+1: берём только t < start - 1 день
        train = (ts < start - day) & panel["target"].notna().to_numpy()
        test = (ts >= start) & (ts < stop)
        if test.any() and train.sum() > 1000:
            model = make_model()
            model.fit(panel.loc[train, FEATURES], y[train])
            part = panel.loc[test, ["ts", "symbol", "target", "fwd"]].copy()
            part["pred"] = model.predict(panel.loc[test, FEATURES])
            out.append(part)
        start = stop
    return Prediction(name, pd.concat(out, ignore_index=True))


def information_coefficient(pred: Prediction) -> dict[str, Any]:
    """Дневная ранговая корреляция прогноза с фактом (метрика качества как в G-Research)."""
    f = pred.frame.dropna(subset=["target"])
    ic = (
        f.groupby("ts")[["pred", "target"]]
        .apply(lambda g: g["pred"].rank().corr(g["target"].rank()) if len(g) >= 5 else np.nan)
        .dropna()
    )
    hit = float((np.sign(f["pred"]) == np.sign(f["target"])).mean())
    t = float(ic.mean() / ic.std(ddof=1) * np.sqrt(len(ic))) if len(ic) > 2 else 0.0
    return {
        "ic_mean": round(float(ic.mean()), 4),
        "ic_t_stat": round(t, 2),
        "days": len(ic),
        "direction_hit_pct": round(hit * 100, 1),
    }


def prediction_weights(
    pred: Prediction, index: pd.Index, columns: pd.Index, n_side: int = 3, smooth: int = 1
) -> pd.DataFrame:
    """Лонг n_side монет с лучшим прогнозом, шорт — с худшим; smooth>1 — сглаживание
    прогноза (EMA) для снижения оборота."""
    p = pred.frame.pivot(index="ts", columns="symbol", values="pred").reindex(columns=columns)
    if smooth > 1:
        p = p.ewm(span=smooth, min_periods=1).mean()
    rank = p.rank(axis=1)
    count = p.notna().sum(axis=1)
    ok = (count >= 2 * n_side).to_numpy()[:, None]
    long = rank.gt(count - n_side, axis=0) & ok
    short = rank.le(n_side, axis=0) & ok
    w = long.astype(float) / (2 * n_side) - short.astype(float) / (2 * n_side)
    return w.reindex(index).fillna(0.0)
