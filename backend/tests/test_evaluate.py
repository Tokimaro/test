import numpy as np
import pandas as pd
import pytest

from app.backtest.evaluate import M15, simulate
from app.domain import Direction
from app.strategy.planner import TradePlan


def plan(direction: Direction = Direction.LONG, tp1: float | None = 103.0) -> TradePlan:
    return TradePlan(
        direction=direction,
        entry=100.0,
        stop=98.0 if direction is Direction.LONG else 102.0,
        tp1=tp1,
        tp2=106.0 if direction is Direction.LONG else 94.0,
        tp1_fraction=0.5 if tp1 else 0.0,
        trailing=True,
        reward_risk=2.25,
        atr=1.0,
        strategy="trend",
    )


def path(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    a = np.array(bars)
    return pd.DataFrame(
        {"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3]},
        index=pd.Index(np.arange(len(bars), dtype="int64") * M15),
    )


def test_stop_first_is_minus_one_r() -> None:
    assert simulate(plan(), 100.0, path([(100, 101, 97.5, 98)]))[:2] == ("stop", -1.0)


def test_stop_and_target_same_bar_is_stop() -> None:
    assert simulate(plan(), 100.0, path([(100, 107, 97, 100)]))[0] == "stop"


def test_tp1_then_breakeven_then_tp2() -> None:
    first, r, _ = simulate(plan(), 100.0, path([(100, 103.5, 99.5, 103), (103, 106.5, 102, 106)]))
    assert first == "target" and r == pytest.approx(0.5 * 1.5 + 0.5 * 3)
    first, r, _ = simulate(plan(), 100.0, path([(100, 103.5, 99.5, 103), (103, 103, 99, 99.5)]))
    assert first == "target" and r == pytest.approx(0.75)  # остаток закрыт в безубыток


def test_short_and_timeout() -> None:
    first, r, hours = simulate(
        plan(Direction.SHORT, tp1=97.0), 100.0, path([(100, 100.5, 99, 99)] * 4)
    )
    assert first == "timeout" and r == pytest.approx(0.5) and hours == 24


def test_gap_through_stop_fills_at_open() -> None:
    assert simulate(plan(), 100.0, path([(96, 97, 95, 96)]))[1] == pytest.approx(-2.0)


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
