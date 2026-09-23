"""Определение режима рынка (раздел 5.3 плана)."""

from enum import StrEnum

import numpy as np
import pandas as pd

from app.trading_config import StrategySettings


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    TRANSITION = "transition"  # ADX между порогами флэта и тренда
    CHAOS = "chaos"  # экстремальная волатильность — не торгуем
    UNKNOWN = "unknown"  # недостаточно данных — не торгуем

    @property
    def tradable(self) -> bool:
        return self not in (Regime.CHAOS, Regime.UNKNOWN)

    @property
    def is_trend(self) -> bool:
        return self in (Regime.TREND_UP, Regime.TREND_DOWN)


def classify_regime(feats: pd.DataFrame, cfg: StrategySettings) -> pd.Series:
    """Режим для каждой строки таблицы признаков (векторно — годится и для бэктеста)."""
    adx = feats["adx"].to_numpy()
    plus_di = feats["plus_di"].to_numpy()
    minus_di = feats["minus_di"].to_numpy()
    vol_pct = feats["natr_pct"].to_numpy()

    unknown = np.isnan(adx) | np.isnan(plus_di) | np.isnan(minus_di) | np.isnan(vol_pct)
    chaos = ~unknown & (vol_pct > cfg.chaos_volatility_percentile)
    trend = ~unknown & ~chaos & (adx > cfg.adx_trend)
    rng = ~unknown & ~chaos & (adx < cfg.adx_range)

    out = np.full(len(feats), Regime.TRANSITION.value, dtype=object)
    out[trend & (plus_di >= minus_di)] = Regime.TREND_UP.value
    out[trend & (plus_di < minus_di)] = Regime.TREND_DOWN.value
    out[rng] = Regime.RANGE.value
    out[chaos] = Regime.CHAOS.value
    out[unknown] = Regime.UNKNOWN.value
    return pd.Series(out, index=feats.index, name="regime")


def higher_tf_bias(feats: pd.DataFrame, neutral_atr: float = 0.5) -> pd.Series:
    """Направление старшего ТФ: +1 цена выше EMA200, -1 ниже, 0 — в пределах
    neutral_atr × ATR от EMA200 (или нет данных)."""
    close = feats["h_close"].to_numpy()
    ema200 = feats["h_ema200"].to_numpy()
    atr = feats["h_atr"].to_numpy()
    diff = close - ema200
    with np.errstate(invalid="ignore"):
        bias = np.where(diff > neutral_atr * atr, 1, np.where(diff < -neutral_atr * atr, -1, 0))
    bias = np.where(np.isnan(diff) | np.isnan(atr), 0, bias)
    return pd.Series(bias, index=feats.index, name="h_bias", dtype="int64")


def long_trend(feats: pd.DataFrame) -> pd.Series:
    """Долгосрочный тренд: +1 — цена выше растущей длинной EMA, -1 — ниже падающей,
    0 — нейтрально (сигналы разнонаправлены или мало истории)."""
    close = feats["h_close"].to_numpy()
    ema = feats["h_ema_long"].to_numpy()
    slope = feats["h_ema_long_slope"].to_numpy()
    with np.errstate(invalid="ignore"):
        up = (close > ema) & (slope > 0)
        down = (close < ema) & (slope < 0)
    trend = np.where(up, 1, np.where(down, -1, 0))
    return pd.Series(trend, index=feats.index, name="long_trend", dtype="int64")
