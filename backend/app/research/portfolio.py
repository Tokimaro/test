"""Векторный бэктест портфельных стратегий на дневных (или 4h) барах.

Стратегия выдаёт матрицу целевых весов W (дата × монета), посчитанную по данным,
известным на закрытии бара t. Веса применяются к доходности следующего бара t → t+1
(сдвиг на один бар — защита от заглядывания в будущее). Издержки:

* комиссия + проскальзывание на оборот: Σ|w_t − w_{t−1}^{дрейф}| × cost_per_side;
* funding перпетуалов: лонги платят, шорты получают funding_per_bar × |w|.

Итог — ряд доходностей портфеля и метрики.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

TAKER_FEE = 0.00055
SLIPPAGE = 0.0005
FUNDING_8H = 0.0001  # базовая ставка Bybit/Binance: 0.01% за 8 часов


@dataclass
class CostModel:
    per_side: float = TAKER_FEE + SLIPPAGE
    funding_8h: float = FUNDING_8H
    perpetual: bool = True  # False — спот (без funding, без шортов)


@dataclass
class Result:
    name: str
    returns: pd.Series  # чистая доходность портфеля за бар
    gross: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    bars_per_year: float
    meta: dict[str, Any] = field(default_factory=dict)


def run_weights(
    name: str,
    weights: pd.DataFrame,
    closes: pd.DataFrame,
    bars_per_year: float,
    costs: CostModel | None = None,
) -> Result:
    costs = costs or CostModel()
    w = weights.reindex(closes.index).fillna(0.0)
    if not costs.perpetual and (w < 0).to_numpy().any():
        raise ValueError("на споте шорты невозможны")
    rets = closes.pct_change().fillna(0.0)
    held = w.shift(1).fillna(0.0)  # вес, выбранный на закрытии t, работает на баре t+1
    gross = (held * rets).sum(axis=1)
    # дрейф весов за бар — оборот считается от фактических весов, а не от целевых
    drifted = held * (1 + rets)
    drifted = drifted.div((1 + gross).replace(0, np.nan), axis=0).fillna(0.0)
    turnover = (w - drifted).abs().sum(axis=1)
    cost = turnover * costs.per_side
    funding: pd.Series | float = 0.0
    if costs.perpetual:
        bars_per_day = bars_per_year / 365
        funding = held.sum(axis=1) * costs.funding_8h * 3 / bars_per_day
    net = gross - cost - funding
    return Result(name, net, gross, w, turnover, bars_per_year)


def metrics(
    r: pd.Series, bars_per_year: float, turnover: pd.Series | None = None
) -> dict[str, Any]:
    r = r.dropna()
    if len(r) < 2:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / bars_per_year
    total = float(eq.iloc[-1] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if eq.iloc[-1] > 0 else -1.0
    dd = float((eq / eq.cummax() - 1).min())
    std = float(r.std(ddof=1))
    sharpe = float(r.mean() / std * np.sqrt(bars_per_year)) if std > 0 else 0.0
    out = {
        "total_return_pct": round(total * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(dd * 100, 1),
        "calmar": round(cagr / -dd, 2) if dd < 0 else None,
        "vol_pct": round(std * np.sqrt(bars_per_year) * 100, 1),
        "hit_rate_pct": round(float((r > 0).mean() * 100), 1),
    }
    if turnover is not None:
        out["turnover_per_year"] = round(float(turnover.loc[r.index].mean() * bars_per_year), 1)
    return out


def sharpe_ci(r: pd.Series, bars_per_year: float, block: int = 20, n: int = 2000) -> list[float]:
    """95% ДИ Sharpe блочным бутстрэпом (блоки по block баров сохраняют автокорреляцию)."""
    x = r.dropna().to_numpy()
    if len(x) < block * 5:
        return []
    rng = np.random.default_rng(0)
    nb = len(x) // block
    starts = np.arange(len(x) - block)
    out = []
    for _ in range(n):
        idx = (rng.choice(starts, nb)[:, None] + np.arange(block)).ravel()
        s = x[idx]
        sd = s.std(ddof=1)
        out.append(s.mean() / sd * np.sqrt(bars_per_year) if sd > 0 else 0.0)
    return [round(float(np.percentile(out, 2.5)), 2), round(float(np.percentile(out, 97.5)), 2)]


def vol_target(
    raw: pd.DataFrame,
    closes: pd.DataFrame,
    target_vol: float,
    bars_per_year: float,
    lookback: int = 30,
    max_gross: float = 2.0,
) -> pd.DataFrame:
    """Масштабирует позиции так, чтобы каждая имела годовую волатильность target_vol,
    а суммарное плечо не превышало max_gross."""
    vol = closes.pct_change().rolling(lookback, min_periods=lookback).std() * np.sqrt(bars_per_year)
    w = raw * (target_vol / vol).clip(upper=5)
    gross = w.abs().sum(axis=1)
    scale = (max_gross / gross).clip(upper=1).fillna(1)
    out: pd.DataFrame = w.mul(scale, axis=0).fillna(0.0)
    return out
