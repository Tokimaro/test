"""Классические систематические стратегии, выбранные по исследованиям.

Обзор источников — docs/research-strategies.md.

Все функции получают матрицу цен закрытия (дата × монета) и возвращают целевые веса,
посчитанные ТОЛЬКО по данным до текущего бара включительно (rolling/shift без будущего).
Параметры взяты из литературы и зафиксированы заранее, а не подобраны по результату.
"""

import numpy as np
import pandas as pd

from app.research.portfolio import vol_target


def hold_every(w: pd.DataFrame, every: int) -> pd.DataFrame:
    """Ребалансировка раз в every баров: между ними целевые веса не меняются."""
    if every <= 1:
        return w
    mask = np.arange(len(w)) % every == 0
    return w.where(pd.Series(mask, index=w.index), np.nan).ffill().fillna(0.0)


def available(closes: pd.DataFrame, min_history: int) -> pd.DataFrame:
    """Монета торгуется и у неё достаточно истории для сигнала."""
    return closes.notna() & (closes.notna().cumsum() >= min_history)


def buy_and_hold(closes: pd.DataFrame, symbols: list[str] | None = None) -> pd.DataFrame:
    cols = symbols or list(closes.columns)
    avail = closes[cols].notna()
    w = avail.div(avail.sum(axis=1), axis=0).fillna(0.0)
    return w.reindex(columns=closes.columns, fill_value=0.0)


def btc_trend_filter(closes: pd.DataFrame, ma: int = 200) -> pd.DataFrame:
    """Классика: держим BTC, пока цена выше 200-дневной средней, иначе — в кэше."""
    btc = closes["BTCUSDT"]
    on = (btc > btc.rolling(ma, min_periods=ma).mean()).astype(float)
    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    w["BTCUSDT"] = on
    return w


def tsmom(
    closes: pd.DataFrame,
    bars_per_year: float,
    lookbacks: tuple[int, ...] = (20, 60, 120),
    long_only: bool = False,
    target_vol: float = 0.25,
    rebalance: int = 7,
) -> pd.DataFrame:
    """Time-series momentum (Moskowitz, Ooi, Pedersen; для крипты — Liu & Tsyvinski и др.):
    знак доходности за несколько горизонтов, усреднённый в сигнал [-1; 1],
    позиция масштабируется по волатильности монеты."""
    sig = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for lb in lookbacks:
        sig = sig + np.sign(closes / closes.shift(lb) - 1)
    sig = sig / len(lookbacks)
    sig = sig.where(available(closes, max(lookbacks) + 1), 0.0).fillna(0.0)
    if long_only:
        sig = sig.clip(lower=0)
    n = available(closes, max(lookbacks) + 1).sum(axis=1).clip(lower=1)
    raw = sig.div(np.sqrt(n), axis=0)  # равный риск на монету, портфель ~ target_vol
    w = vol_target(raw, closes, target_vol, bars_per_year, max_gross=2.0 if not long_only else 1.0)
    return hold_every(w, rebalance)


def xs_momentum(
    closes: pd.DataFrame,
    bars_per_year: float,
    lookback: int = 28,
    n_side: int = 3,
    rebalance: int = 7,
    vol_managed: bool = True,
    target_vol: float = 0.3,
) -> pd.DataFrame:
    """Кросс-секционный моментум (Liu, Tsyvinski, Wu 2022): лонг лидеров за lookback,
    шорт аутсайдеров, рыночно-нейтрально. С vol_managed — масштаб по недавней волатильности
    самой стратегии (снижает «краши» моментума)."""
    past = closes / closes.shift(lookback) - 1
    past = past.where(available(closes, lookback + 1))
    rank = past.rank(axis=1)
    count = past.notna().sum(axis=1)
    long = rank.gt(count - n_side, axis=0) & (count >= 2 * n_side).to_numpy()[:, None]
    short = rank.le(n_side, axis=0) & (count >= 2 * n_side).to_numpy()[:, None]
    w = long.astype(float) / (2 * n_side) - short.astype(float) / (2 * n_side)
    w = hold_every(w, rebalance)
    if vol_managed:
        rets = closes.pct_change().fillna(0.0)
        strat = (w.shift(1) * rets).sum(axis=1)
        vol = strat.rolling(60, min_periods=30).std().shift(1) * np.sqrt(bars_per_year)
        w = w.mul((target_vol / vol).clip(upper=2).fillna(0.0), axis=0)
    return w


def st_reversal(closes: pd.DataFrame, n_side: int = 3) -> pd.DataFrame:
    """Краткосрочный разворот: вчерашние аутсайдеры — лонг, лидеры — шорт. Ежедневно."""
    past = (closes / closes.shift(1) - 1).where(available(closes, 30))
    rank = past.rank(axis=1)
    count = past.notna().sum(axis=1)
    ok = (count >= 2 * n_side).to_numpy()[:, None]
    long = rank.le(n_side, axis=0) & ok
    short = rank.gt(count - n_side, axis=0) & ok
    return long.astype(float) / (2 * n_side) - short.astype(float) / (2 * n_side)
