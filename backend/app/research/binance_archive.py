"""Загрузка свечей из публичного архива Binance (data.binance.vision) в формат датасета.

Архив доступен и через S3-эндпоинт бакета. Файлы — помесячные ZIP с CSV:
    data/futures/um/monthly/klines/<SYMBOL>/<interval>/<SYMBOL>-<interval>-YYYY-MM.zip
Результат пишется в раскладку, которую читает app.backtest.dataset.load_candles:
    <root>/data/interval_id=<interval>/symbol_id=<SYMBOL>/year=YYYY/month=MM/<...>.parquet

    uv run --extra research python -m app.research.binance_archive \\
        --root ../data/binance_um --interval 1m --start 2023-01 --end 2026-08 1000PEPEUSDT WIFUSDT
"""

import argparse
import io
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pandas as pd

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
MARKETS = {"um": "data/futures/um/monthly/klines", "spot": "data/spot/monthly/klines"}
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]


def available_months(client: httpx.Client, symbol: str, interval: str, market: str) -> list[str]:
    prefix = f"{MARKETS[market]}/{symbol}/{interval}/"
    text = client.get(BUCKET, params={"prefix": prefix, "delimiter": "/"}).text
    return sorted(set(re.findall(rf"{symbol}-{interval}-(\d{{4}}-\d{{2}})\.zip<", text)))


def parse_csv(raw: bytes) -> pd.DataFrame:
    """CSV архива → свечи. Заголовок есть не во всех файлах; время бывает в мс или мкс."""
    first = raw.split(b"\n", 1)[0]
    header = 0 if first[:1].isalpha() else None
    df = pd.read_csv(io.BytesIO(raw), header=header, usecols=range(8))
    df.columns = COLUMNS
    t = df["open_time"].to_numpy(dtype="int64")
    ts = pd.to_datetime(t, unit="us" if t[0] > 10**14 else "ms", utc=True)
    return pd.DataFrame(
        {
            "timestamp": ts,
            **{c: df[c].astype("float64") for c in ("open", "high", "low", "close", "volume")},
            "quote_volume": df["quote_volume"].astype("float64"),
        }
    )


def target(root: Path, symbol: str, interval: str, month: str) -> Path:
    y, m = month.split("-")
    folder = root / "data" / f"interval_id={interval}" / f"symbol_id={symbol}"
    return folder / f"year={y}" / f"month={m}" / f"{symbol}-{interval}-{month}.parquet"


def download(
    root: Path,
    symbols: list[str],
    interval: str,
    start: str,
    end: str,
    market: str = "um",
    workers: int = 8,
) -> dict[str, int]:
    """Скачивает недостающие месяцы [start; end]; возвращает число месяцев по символам."""
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        jobs = []
        for s in symbols:
            for month in available_months(client, s, interval, market):
                if start <= month <= end and not target(root, s, interval, month).exists():
                    jobs.append((s, month))

        def fetch(job: tuple[str, str]) -> None:
            s, month = job
            url = f"{BUCKET}/{MARKETS[market]}/{s}/{interval}/{s}-{interval}-{month}.zip"
            for attempt in range(4):
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    break
                except httpx.HTTPError:
                    if attempt == 3:
                        raise
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                df = parse_csv(z.read(z.namelist()[0]))
            path = target(root, s, interval, month)
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)

        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(fetch, jobs))
    counts = {}
    for s in symbols:
        folder = root / "data" / f"interval_id={interval}" / f"symbol_id={s}"
        counts[s] = len(list(folder.glob("year=*/month=*/*.parquet")))
    return counts


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("symbols", nargs="+")
    p.add_argument("--root", required=True)
    p.add_argument("--interval", default="1m")
    p.add_argument("--start", default="2023-01")
    p.add_argument("--end", default="2026-08")
    p.add_argument("--market", choices=sorted(MARKETS), default="um")
    args = p.parse_args()
    print(download(Path(args.root), args.symbols, args.interval, args.start, args.end, args.market))


if __name__ == "__main__":
    main()
