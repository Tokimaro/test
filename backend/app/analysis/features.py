"""Сборка таблицы признаков по трём таймфреймам (раздел 5.2 плана).

Главная опасность мультитаймфреймового анализа — заглядывание в будущее: 4h-свеча,
открывшаяся в 12:00, известна только в 16:00. align_closed() присоединяет к строке рабочего
таймфрейма только те свечи старшего/младшего ТФ, которые уже ЗАКРЫЛИСЬ к моменту
закрытия рабочей свечи.
"""

import numpy as np
import pandas as pd

from app.analysis import indicators as ind
from app.domain import Timeframe
from app.trading_config import StrategySettings

# Минимум истории рабочего ТФ, после которого признаки считаются надёжными
MIN_WARMUP_BARS = 250


def align_closed(
    base: pd.DataFrame,
    base_tf: Timeframe,
    other: pd.DataFrame,
    other_tf: Timeframe,
    columns: list[str],
    prefix: str,
) -> pd.DataFrame:
    base_close = base.index.to_numpy(dtype="int64") + base_tf.ms
    other_close = other.index.to_numpy(dtype="int64") + other_tf.ms
    # индекс последней свечи other, закрытой не позже закрытия base
    pos = np.searchsorted(other_close, base_close, side="right") - 1
    out = pd.DataFrame(index=base.index)
    for col in columns:
        values = other[col].to_numpy(dtype="float64")
        aligned = np.where(pos >= 0, values[np.clip(pos, 0, None)], np.nan)
        out[f"{prefix}{col}"] = aligned
    return out


def working_features(df: pd.DataFrame, tf: Timeframe, cfg: StrategySettings) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["ema21"] = ind.ema(close, 21)
    out["ema55"] = ind.ema(close, 55)
    out["ema200"] = ind.ema(close, 200)
    out["ema21_slope"] = out["ema21"].diff(3)
    out["ema55_slope"] = out["ema55"].diff(3)
    out["rsi"] = ind.rsi(close, 14)
    out["atr"] = ind.atr(out, 14)
    out["natr"] = out["atr"] / close
    lookback = max(50, cfg.volatility_lookback_days * 86_400 // tf.seconds)
    out["natr_pct"] = ind.rolling_percentile(
        out["natr"], lookback, min_periods=max(50, lookback // 4)
    )
    adx = ind.adx(out, 14)
    out["adx"], out["plus_di"], out["minus_di"] = adx["adx"], adx["plus_di"], adx["minus_di"]
    macd = ind.macd(close)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd["macd"], macd["signal"], macd["hist"]
    bb = ind.bollinger(close, 20, 2.0)
    out["bb_mid"], out["bb_upper"], out["bb_lower"] = bb["mid"], bb["upper"], bb["lower"]
    out["bb_width"] = bb["width"]
    out["bb_width_pct"] = ind.rolling_percentile(bb["width"], 120, min_periods=120)
    don = ind.donchian(out, 20)
    out["don_upper"], out["don_lower"] = don["upper"], don["lower"]
    out["vwap"] = ind.rolling_vwap(out, 24)
    out["vol_sma"] = ind.sma(out["volume"], 20)
    ch = ind.chandelier(out, 22, cfg.stops.trailing_atr)
    out["chand_long"], out["chand_short"] = ch["long"], ch["short"]
    sw = ind.swing_points(out, 3, 3)
    out["swing_high"], out["swing_low"] = sw["swing_high"], sw["swing_low"]
    # RSI в момент последнего подтверждённого свинга — для поиска дивергенций
    out["rsi_at_swing_low"] = _value_at_swing(out["rsi"], sw["swing_low"])
    out["rsi_at_swing_high"] = _value_at_swing(out["rsi"], sw["swing_high"])
    return out


def _value_at_swing(values: pd.Series, swing: pd.Series, right: int = 3) -> pd.Series:
    """Значение индикатора в баре свинга (свинг подтверждается через right баров)."""
    changed = swing.ne(swing.shift(1)) & swing.notna()
    at_pivot = values.shift(right).where(changed)
    return at_pivot.ffill()


def higher_features(
    df: pd.DataFrame, tf: Timeframe = Timeframe.H4, long_trend_days: int = 200
) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"]
    out["ema200"] = ind.ema(df["close"], 200)
    out["ema50"] = ind.ema(df["close"], 50)
    out["atr"] = ind.atr(df, 14)
    # долгосрочный тренд: EMA за long_trend_days и её изменение за 30 дней
    bars_per_day = max(1, 86_400 // tf.seconds)
    out["ema_long"] = ind.ema(df["close"], long_trend_days * bars_per_day)
    out["ema_long_slope"] = out["ema_long"].diff(30 * bars_per_day)
    return out


def entry_features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    out["close"] = df["close"]
    out["open"] = df["open"]
    out["ema21"] = ind.ema(df["close"], 21)
    out["rsi"] = ind.rsi(df["close"], 14)
    return out


def build_features(
    working: pd.DataFrame,
    working_tf: Timeframe,
    higher: pd.DataFrame,
    higher_tf: Timeframe,
    entry: pd.DataFrame | None,
    entry_tf: Timeframe | None,
    cfg: StrategySettings,
) -> pd.DataFrame:
    feats = working_features(working, working_tf, cfg)
    hf = higher_features(higher, higher_tf, cfg.long_trend_days)
    cols = ["close", "ema200", "ema50", "atr", "ema_long", "ema_long_slope"]
    feats = feats.join(align_closed(feats, working_tf, hf, higher_tf, cols, "h_"))
    if entry is not None and entry_tf is not None and not entry.empty:
        ef = entry_features(entry)
        feats = feats.join(
            align_closed(feats, working_tf, ef, entry_tf, ["close", "open", "ema21", "rsi"], "e_")
        )
    else:
        for col in ("e_close", "e_open", "e_ema21", "e_rsi"):
            feats[col] = np.nan
    return feats
