"""Генераторы синтетических OHLCV-рядов для тестов."""

import numpy as np
import pandas as pd

from app.domain import Candle, Timeframe

T0 = 1_700_000_000_000 // Timeframe.D1.ms * Timeframe.D1.ms


def make_ohlcv(
    n: int,
    *,
    drift: float = 0.0,
    vol: float = 0.01,
    seed: int = 1,
    start_price: float = 100.0,
    tf: Timeframe = Timeframe.H1,
    start_ts: int = T0,
    drifts: np.ndarray | None = None,
    vols: np.ndarray | None = None,
) -> pd.DataFrame:
    """Геометрическое блуждание. drifts/vols позволяют задать режимы по участкам."""
    rng = np.random.default_rng(seed)
    mu = drifts if drifts is not None else np.full(n, drift)
    sigma = vols if vols is not None else np.full(n, vol)
    rets = rng.normal(mu, sigma)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.r_[start_price, close[:-1]]
    wick = np.abs(rng.normal(0, sigma * 0.6))
    high = np.maximum(open_, close) * (1 + wick)
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, sigma * 0.6)))
    volume = rng.uniform(50, 150, n) * (1 + 5 * np.abs(rets) / sigma.mean())
    idx = pd.Index(start_ts + np.arange(n, dtype="int64") * tf.ms, name="ts")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


def resample(df: pd.DataFrame, src: Timeframe, dst: Timeframe) -> pd.DataFrame:
    """Агрегирует свечи src в dst (dst кратен src). Неполная последняя группа отбрасывается."""
    factor = dst.ms // src.ms
    bucket = (df.index.to_numpy() // dst.ms) * dst.ms
    g = df.groupby(bucket)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
        }
    )
    counts = g.size()
    out = out[counts == factor]
    out.index = pd.Index(out.index.astype("int64"), name="ts")
    return out


def frame_to_candles(df: pd.DataFrame) -> list[Candle]:
    cols = [df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume")]
    return [
        Candle(ts=int(ts), open=o, high=h, low=lo, close=c, volume=v)
        for ts, o, h, lo, c, v in zip(df.index, *cols, strict=True)
    ]
