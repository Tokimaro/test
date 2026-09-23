"""Загрузка свечей из Parquet-датасета с разбиением по интервалу/символу/месяцу.

Формат совместим с публичным датасетом Binance
https://github.com/Speirsy11/crypto-dataset :
    data/interval_id=1h/symbol_id=BTCUSDT/year=2026/month=09/BTCUSDT-1h-2026-09.parquet
колонки: timestamp (UTC, открытие свечи), open, high, low, close, volume.
Нужен pyarrow: `uv sync --extra research`.
"""

from pathlib import Path

import pandas as pd

from app.domain import Timeframe

INTERVALS = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
}


def load_candles(root: Path, symbol: str, tf: Timeframe) -> pd.DataFrame:
    """Свечи символа в формате проекта: индекс ts (мс, int64), open/high/low/close/volume."""
    folder = root / "data" / f"interval_id={INTERVALS[tf]}" / f"symbol_id={symbol}"
    files = sorted(folder.glob("year=*/month=*/*.parquet"))
    if not files:
        raise FileNotFoundError(f"нет данных {symbol} {INTERVALS[tf]} в {folder}")
    raw = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    ts = pd.to_datetime(raw["timestamp"], utc=True)
    df = pd.DataFrame(
        {
            c: raw[c].astype("float64").to_numpy()
            for c in ("open", "high", "low", "close", "volume")
        },
        # точная конвертация в мс, не зависящая от единицы хранения (ns/us) в Parquet
        index=pd.Index(
            ((ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).astype("int64"),
            name="ts",
        ),
    )
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # проверка целостности: свеча не может быть вне своего диапазона
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (
        df["low"] > df[["open", "close"]].min(axis=1)
    )
    if bad.any():
        raise ValueError(f"{symbol} {tf.value}: {int(bad.sum())} некорректных свечей")
    return df


def available_symbols(root: Path, tf: Timeframe = Timeframe.H1) -> list[str]:
    folder = root / "data" / f"interval_id={INTERVALS[tf]}"
    return sorted(p.name.split("=", 1)[1] for p in folder.glob("symbol_id=*"))
