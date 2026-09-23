from collections.abc import Sequence

import numpy as np
import pandas as pd
import pytest

from app.backtest.evaluate import M15, Path15, resolve
from app.domain import Direction
from app.strategy.planner import TradePlan


def plan(direction: Direction = Direction.LONG, tp1: float | None = 103.0) -> TradePlan:
    long = direction is Direction.LONG
    return TradePlan(
        direction=direction,
        entry=100.0,
        stop=98.0 if long else 102.0,
        tp1=tp1,
        tp2=106.0 if long else 94.0,
        tp1_fraction=0.5 if tp1 else 0.0,
        trailing=True,
        reward_risk=2.25,
        atr=1.0,
        strategy="trend",
    )


def path(bars: Sequence[tuple[float, float, float, float]]) -> Path15:
    a = np.array(bars, dtype=float)
    return Path15(
        pd.DataFrame(
            {"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3]},
            index=pd.Index(np.arange(len(bars), dtype="int64") * M15),
        )
    )


def test_stop_first_is_minus_one_r() -> None:
    r = resolve(plan(), 100.0, 0, path([(100, 101, 99, 100), (100, 101, 97.5, 98)]))
    assert r is not None and (r.kind, r.r, r.exit_idx) == ("sl", -1.0, 1)


def test_stop_and_target_same_bar_is_stop() -> None:
    r = resolve(plan(), 100.0, 0, path([(100, 107, 97, 100)]))
    assert r is not None and r.kind == "sl"


def test_tp1_then_tp2_and_tp1_then_breakeven() -> None:
    r = resolve(plan(), 100.0, 0, path([(100, 103.5, 99.5, 103), (103, 106.5, 102, 106)]))
    assert r is not None and r.kind == "tp" and r.r == pytest.approx(0.5 * 1.5 + 0.5 * 3)
    r = resolve(plan(), 100.0, 0, path([(100, 103.5, 99.5, 103), (103, 103, 99, 99.5)]))
    assert r is not None and r.kind == "tp1_be" and r.r == pytest.approx(0.75)


def test_trade_runs_until_resolution_without_time_limit() -> None:
    bars = [(100, 100.5, 99.5, 100)] * 5000 + [(100, 106.5, 99.5, 106)]
    r = resolve(plan(), 100.0, 0, path(bars))
    assert r is not None and r.kind == "tp" and r.exit_idx == 5000


def test_unresolved_at_data_end_is_none() -> None:
    assert resolve(plan(), 100.0, 0, path([(100, 100.5, 99.5, 100)] * 10)) is None


def test_short_single_target_and_gap() -> None:
    mr = TradePlan(
        Direction.SHORT, 100.0, 102.0, None, 97.0, 0.0, False, 1.5, 1.0, "mean_reversion"
    )
    r = resolve(mr, 100.0, 0, path([(100, 100.5, 96.5, 97)]))
    assert r is not None and r.kind == "tp" and r.r == pytest.approx(1.5)
    r = resolve(plan(), 100.0, 0, path([(96, 97, 95, 96)]))
    assert r is not None and r.r == pytest.approx(-2.0)  # гэп через стоп — по open


def test_dataset_loader_roundtrip(tmp_path: pytest.TempPathFactory) -> None:
    pytest.importorskip("pyarrow")
    from app.backtest.dataset import load_candles
    from app.domain import Timeframe

    folder = tmp_path / "data/interval_id=1h/symbol_id=BTCUSDT/year=2026/month=09"  # type: ignore[operator]
    folder.mkdir(parents=True)
    ts = pd.date_range("2026-09-01", periods=3, freq="h", tz="UTC").astype("datetime64[us, UTC]")
    pd.DataFrame(
        {
            "timestamp": ts,
            "open": [1.0, 2, 3],
            "high": [2.0, 3, 4],
            "low": [0.5, 1, 2],
            "close": [1.5, 2.5, 3.5],
            "volume": [1.0, 1, 1],
        }
    ).to_parquet(folder / "x.parquet")
    df = load_candles(tmp_path, "BTCUSDT", Timeframe.H1)  # type: ignore[arg-type]
    assert list(df.index) == [1788220800000, 1788224400000, 1788228000000]
