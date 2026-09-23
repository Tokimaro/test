"""Технические индикаторы на numpy/pandas.

Все функции причинные: значение в строке i зависит только от данных до i включительно,
поэтому индикаторы можно один раз посчитать на всей истории в бэктесте без заглядывания
в будущее. Формулы совпадают с TA-Lib (проверяется в тестах), период прогрева — NaN.
"""

import numpy as np
import pandas as pd

from app.domain import Candle

FloatArray = np.ndarray


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        },
        index=pd.Index([c.ts for c in candles], name="ts", dtype="int64"),
        dtype="float64",
    )
    return df


def _values(s: pd.Series) -> FloatArray:
    return s.to_numpy(dtype="float64", na_value=np.nan)


def _first_valid(x: FloatArray) -> int:
    valid = np.flatnonzero(~np.isnan(x))
    return int(valid[0]) if valid.size else len(x)


def _seeded_smoothing(x: FloatArray, n: int, alpha: float) -> FloatArray:
    """Экспоненциальное сглаживание, стартующее со SMA первых n валидных значений (как TA-Lib)."""
    out = np.full_like(x, np.nan)
    start = _first_valid(x)
    seed_end = start + n
    if seed_end > len(x):
        return out
    prev = float(np.mean(x[start:seed_end]))
    out[seed_end - 1] = prev
    for i in range(seed_end, len(x)):
        prev = prev + alpha * (x[i] - prev)
        out[i] = prev
    return out


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return pd.Series(_seeded_smoothing(_values(s), n, 2.0 / (n + 1)), index=s.index)


def wilder(s: pd.Series, n: int) -> pd.Series:
    """Сглаживание Уайлдера (RMA), alpha = 1/n."""
    return pd.Series(_seeded_smoothing(_values(s), n, 1.0 / n), index=s.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    gain = wilder(diff.clip(lower=0), n)
    loss = wilder((-diff).clip(lower=0), n)
    total = gain + loss
    out = 100.0 * gain / total
    return out.where(total != 0, 50.0).where(gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1, skipna=False)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """ADX и направленные индикаторы +DI/-DI по алгоритму TA-Lib."""
    high, low, close = _values(df["high"]), _values(df["low"]), _values(df["close"])
    size = len(close)
    plus_di = np.full(size, np.nan)
    minus_di = np.full(size, np.nan)
    adx_out = np.full(size, np.nan)
    if size < 2 * n:
        return pd.DataFrame(
            {"adx": adx_out, "plus_di": plus_di, "minus_di": minus_di}, index=df.index
        )

    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    pdm = np.where((up > down) & (up > 0), up, 0.0)
    mdm = np.where((down > up) & (down > 0), down, 0.0)
    tr = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])]
    )
    # индекс k в массивах pdm/mdm/tr соответствует строке k+1 исходных данных
    s_pdm = float(np.sum(pdm[: n - 1]))
    s_mdm = float(np.sum(mdm[: n - 1]))
    s_tr = float(np.sum(tr[: n - 1]))
    dx = np.full(size, np.nan)
    for k in range(n - 1, size - 1):
        s_pdm = s_pdm - s_pdm / n + pdm[k]
        s_mdm = s_mdm - s_mdm / n + mdm[k]
        s_tr = s_tr - s_tr / n + tr[k]
        i = k + 1
        if s_tr > 0:
            p, m = 100.0 * s_pdm / s_tr, 100.0 * s_mdm / s_tr
        else:
            p = m = 0.0
        plus_di[i], minus_di[i] = p, m
        dx[i] = 100.0 * abs(p - m) / (p + m) if (p + m) > 0 else 0.0

    first = 2 * n - 1
    prev = float(np.mean(dx[n : first + 1]))
    adx_out[first] = prev
    for i in range(first + 1, size):
        prev = (prev * (n - 1) + dx[i]) / n
        adx_out[i] = prev
    return pd.DataFrame({"adx": adx_out, "plus_di": plus_di, "minus_di": minus_di}, index=df.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    # Как в TA-Lib: быстрая EMA стартует в той же точке, что и медленная (со SMA последних
    # fast значений окна медленной), а линия MACD отдаётся с появлением сигнальной.
    values = _values(close)
    start = _first_valid(values)
    fast_input = values.copy()
    fast_input[: start + slow - fast] = np.nan
    fast_ema = _seeded_smoothing(fast_input, fast, 2.0 / (fast + 1))
    slow_ema = _seeded_smoothing(values, slow, 2.0 / (slow + 1))
    line = pd.Series(fast_ema - slow_ema, index=close.index)
    sig = ema(line, signal)
    line = line.where(sig.notna())
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig}, index=close.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * std, mid - k * std
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "width": (upper - lower) / mid},
        index=close.index,
    )


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Канал по ПРЕДЫДУЩИМ n свечам (текущая не входит) — для проверки пробоя."""
    upper = df["high"].shift(1).rolling(n, min_periods=n).max()
    lower = df["low"].shift(1).rolling(n, min_periods=n).min()
    return pd.DataFrame({"upper": upper, "lower": lower}, index=df.index)


def rolling_vwap(df: pd.DataFrame, n: int = 24) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (typical * df["volume"]).rolling(n, min_periods=n).sum()
    vol = df["volume"].rolling(n, min_periods=n).sum()
    return (pv / vol).where(vol > 0)


def chandelier(df: pd.DataFrame, n: int = 22, mult: float = 3.0) -> pd.DataFrame:
    a = atr(df, n)
    long_stop = df["high"].rolling(n, min_periods=n).max() - mult * a
    short_stop = df["low"].rolling(n, min_periods=n).min() + mult * a
    return pd.DataFrame({"long": long_stop, "short": short_stop}, index=df.index)


def rolling_percentile(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """Процентиль (0..100) текущего значения среди последних window значений, включая текущее.
    NaN внутри окна игнорируются."""

    def pct(w: FloatArray) -> float:
        cur = w[-1]
        valid = w[~np.isnan(w)]
        if np.isnan(cur) or valid.size == 0:
            return np.nan
        return float(np.sum(valid <= cur) / valid.size * 100.0)

    return s.rolling(window, min_periods=min_periods or window).apply(pct, raw=True)


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Последние ПОДТВЕРЖДЁННЫЕ свинг-экстремумы на каждой строке.

    Пивот в строке j подтверждается только в строке j+right, поэтому в строке i видны лишь
    пивоты с j <= i-right — заглядывания вперёд нет.
    """
    high, low = _values(df["high"]), _values(df["low"])
    size = len(high)
    swing_high = np.full(size, np.nan)
    swing_low = np.full(size, np.nan)
    last_h = last_l = np.nan
    for i in range(size):
        j = i - right
        if j - left >= 0:
            window_h = high[j - left : j + right + 1]
            window_l = low[j - left : j + right + 1]
            if high[j] == window_h.max() and np.argmax(window_h) == left:
                last_h = high[j]
            if low[j] == window_l.min() and np.argmin(window_l) == left:
                last_l = low[j]
        swing_high[i], swing_low[i] = last_h, last_l
    return pd.DataFrame({"swing_high": swing_high, "swing_low": swing_low}, index=df.index)
