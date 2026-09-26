"""Свечи → DataFrame (индекс — ts открытия в мс)."""

import pandas as pd

from app.domain import Candle


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
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
