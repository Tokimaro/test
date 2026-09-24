import pandas as pd

from app.research.binance_archive import parse_csv, target

PRICES = "0.6293,0.6637,0.5889,0.6111,367513681.6"
ROW_MS = f"1740787200000,{PRICES},1740873599999,229251670.45,939938,1,1,0\n"
ROW_US = f"1740787200000000,{PRICES},1740873599999999,229251670.45,9,1,1,0\n"
HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore\n"
)


def test_parse_with_header_and_ms() -> None:
    df = parse_csv((HEADER + ROW_MS).encode())
    assert df["timestamp"].iloc[0] == pd.Timestamp("2025-03-01", tz="UTC")
    assert df["close"].iloc[0] == 0.6111
    assert list(df.columns) == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
    ]


def test_parse_without_header_and_microseconds() -> None:
    df = parse_csv(ROW_US.encode())
    assert df["timestamp"].iloc[0] == pd.Timestamp("2025-03-01", tz="UTC")
    assert len(df) == 1


def test_target_layout_matches_dataset_loader(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = target(tmp_path, "WIFUSDT", "1m", "2025-03")
    assert p.relative_to(tmp_path).as_posix() == (
        "data/interval_id=1m/symbol_id=WIFUSDT/year=2025/month=03/WIFUSDT-1m-2025-03.parquet"
    )
