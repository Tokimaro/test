from dataclasses import replace
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pytest

from app.analysis.regime import Regime
from app.backtest.engine import (
    FUNDING_PERIOD_MS,
    Backtester,
    BacktestResult,
    BacktestSettings,
    PreparedSymbol,
    SymbolData,
)
from app.backtest.metrics import monte_carlo_drawdown, summarize
from app.backtest.walk_forward import walk_forward
from app.config import BACKEND_DIR
from app.domain import Direction, Instrument, MarketType, Timeframe
from app.strategy.ensemble import Signal
from app.trading_config import TradingConfig
from tests.synthetic import make_ohlcv, resample

H = Timeframe.H1.ms
T0 = 1_700_006_400_000  # кратно 8 часам (00:00 UTC)
CONFIG = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml")
MARKET = CONFIG.markets["crypto"]

INST = Instrument(
    symbol="TEST",
    market_type=MarketType.CRYPTO,
    category="linear",
    tick_size=Decimal("0.01"),
    qty_step=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    max_qty=Decimal(1_000_000),
    max_leverage=Decimal(50),
    taker_fee=Decimal(0),
)


def manual_symbol(
    bars: list[tuple[float, float, float, float]],
    signal_at: int = 0,
    direction: Direction = Direction.LONG,
    atr: float = 2.0,
    strategy: str = "trend",
) -> PreparedSymbol:
    """Свечи (o, h, l, c) и сильный сигнал на закрытии свечи signal_at."""
    n = len(bars)
    arr = np.array(bars, dtype=float)
    nan = np.full(n, np.nan)
    cols = {
        "open": arr[:, 0],
        "high": arr[:, 1],
        "low": arr[:, 2],
        "close": arr[:, 3],
        "atr": np.full(n, atr),
        "swing_low": nan,
        "swing_high": nan,
        "bb_mid": nan,
        "chand_long": nan,
        "chand_short": nan,
    }
    index = T0 + np.arange(n, dtype="int64") * H
    signals = [
        Signal(
            int(index[i]),
            "TEST",
            direction if i == signal_at else None,
            90.0 if i == signal_at else 0.0,
            Regime.TREND_UP,
            strategy,
        )
        for i in range(n)
    ]
    return PreparedSymbol("TEST", INST, index, cols, signals)


def run(
    sym: PreparedSymbol, taker_fee: float | None = None, slippage_pct: float = 0.0
) -> BacktestResult:
    settings = BacktestSettings(
        initial_equity=10_000,
        slippage_pct=slippage_pct,
        funding_rate_8h=0.0,
        taker_fee=taker_fee,
    )
    return Backtester(CONFIG, MARKET, settings).run([sym])


FLAT = (100.0, 100.5, 99.5, 100.0)
# вход по открытию свечи 1 = 100; стоп 97 (1.5 ATR), TP1 104.5, TP2 109


def test_stop_loss_is_minus_one_r() -> None:
    res = run(manual_symbol([FLAT, FLAT, (100, 100.5, 96, 96.5), FLAT]))
    (t,) = res.trades
    assert t.close_reason == "sl"
    assert t.exit == pytest.approx(97.0)
    assert t.r_multiple == pytest.approx(-1.0, abs=0.01)
    assert t.risk_amount <= 100.0  # 1% от 10k
    assert res.final_equity == pytest.approx(10_000 + t.pnl)


def test_stop_and_target_in_same_bar_counts_as_stop() -> None:
    res = run(manual_symbol([FLAT, FLAT, (100, 110, 96, 105), FLAT]))
    assert res.trades[0].close_reason == "sl"


def test_tp1_then_tp2() -> None:
    res = run(manual_symbol([FLAT, FLAT, (100, 105, 99.8, 104.8), (104.8, 110, 104, 109.5), FLAT]))
    (t,) = res.trades
    assert t.close_reason == "tp2"
    # половина на 1.5R, половина на 3R
    assert t.r_multiple == pytest.approx(2.25, abs=0.03)


def test_tp1_then_breakeven_in_bearish_bar() -> None:
    # медвежья свеча: сначала high (TP1), затем low ниже безубытка
    res = run(manual_symbol([FLAT, FLAT, (101, 105, 99.9, 100.2), FLAT]))
    (t,) = res.trades
    assert t.close_reason == "be"
    assert t.r_multiple == pytest.approx(0.75, abs=0.02)  # 0.5 × 1.5R + 0.5 × 0


def test_tp1_in_bullish_bar_keeps_position() -> None:
    # бычья свеча: low был до high → безубыток в этой свече не срабатывает
    res = run(manual_symbol([FLAT, FLAT, (100, 105, 99.9, 104.9), *[FLAT] * 3]))
    (t,) = res.trades
    # закрыт по безубытку на следующей свече, а не в свече TP1
    assert t.close_reason == "be"
    assert t.bars_held >= 2


def test_gap_through_stop_fills_at_open() -> None:
    res = run(manual_symbol([FLAT, FLAT, (94, 95, 93, 94.5), FLAT]))
    (t,) = res.trades
    assert t.exit == pytest.approx(94.0)
    assert t.r_multiple < -1.5


def test_signal_invalidated_by_gap_is_skipped() -> None:
    res = run(manual_symbol([FLAT, (96, 97, 95, 96)]))
    assert res.trades == []
    assert res.signal_stats.get("skip_gap") == 1


def test_time_stop() -> None:
    res = run(manual_symbol([FLAT] * 40))
    (t,) = res.trades
    assert t.close_reason == "time"
    assert t.bars_held == CONFIG.strategy.stops.time_stop_bars


def test_short_mirror() -> None:
    res = run(
        manual_symbol(
            [FLAT, FLAT, (100, 100.2, 95, 95.3), (95.3, 96, 90, 90.5), FLAT],
            direction=Direction.SHORT,
        )
    )
    (t,) = res.trades
    assert t.direction == "short"
    assert t.close_reason == "tp2"
    assert t.r_multiple == pytest.approx(2.25, abs=0.03)


def test_fees_and_slippage_reduce_pnl_and_are_accounted() -> None:
    bars = [FLAT, FLAT, (100, 105, 99.8, 104.8), (104.8, 110, 104, 109.5), FLAT]
    clean = run(manual_symbol(bars))
    costly = run(manual_symbol(bars), taker_fee=0.001, slippage_pct=0.001)
    assert costly.trades[0].pnl < clean.trades[0].pnl
    assert costly.trades[0].fees > 0
    assert costly.final_equity == pytest.approx(10_000 + costly.trades[0].pnl)


def test_funding_charged_every_8h_for_longs() -> None:
    bars = [FLAT] * 20
    sym = manual_symbol(bars)
    settings = BacktestSettings(slippage_pct=0, funding_rate_8h=0.001)
    res = Backtester(CONFIG, MARKET, settings).run([sym])
    (t,) = res.trades
    boundaries = sum(
        1 for i in range(2, 20) if (T0 + i * H) % FUNDING_PERIOD_MS == 0 and i < 1 + t.bars_held
    )
    assert boundaries >= 1
    assert t.funding == pytest.approx(boundaries * t.qty * 100 * 0.001, rel=1e-6)


def test_mean_reversion_single_target() -> None:
    sym = manual_symbol([FLAT, FLAT, (100, 108.5, 99.5, 108), FLAT], strategy="mean_reversion")
    sym.cols["bb_mid"] = np.full(4, 108.0)
    res = run(sym)
    (t,) = res.trades
    assert t.close_reason == "tp2"
    assert t.r_multiple == pytest.approx(8 / 3, abs=0.02)


# ------------------------------------------------------------------ интеграция
@pytest.fixture(scope="module")
def synthetic_run():  # type: ignore[no-untyped-def]
    n = 6000
    rng = np.random.default_rng(3)
    data = []
    for k, sym in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT"]):
        # чередующиеся трендовые и боковые участки
        drifts = np.repeat(rng.choice([-0.002, 0.0, 0.0, 0.002], size=n // 500), 500)
        h1 = make_ohlcv(n, drifts=drifts, vol=0.008, seed=10 + k, start_ts=T0)
        data.append(
            SymbolData(
                sym,
                replace(INST, symbol=sym, taker_fee=Decimal("0.00055")),
                h1,
                resample(h1, Timeframe.H1, Timeframe.H4),
                None,
            )
        )
    bt = Backtester(CONFIG, MARKET, BacktestSettings())
    prepared = bt.prepare(data)
    return bt, prepared, bt.run(prepared)


def test_integration_invariants(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, _, res = synthetic_run
    assert len(res.trades) > 20
    # капитал сходится с суммой сделок
    assert res.final_equity == pytest.approx(10_000 + sum(t.pnl for t in res.trades), rel=1e-9)
    # потеря на сделку не больше ~1R (+ издержки, funding), гэпов в синтетике нет
    assert min(t.r_multiple for t in res.trades) > -1.3
    # по каждому инструменту позиции не пересекаются во времени
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        ts = sorted((t.entry_ts, t.exit_ts) for t in res.trades if t.symbol == sym)
        assert all(a[1] <= b[0] for a, b in pairwise(ts))
    # одновременно открыто не больше max_open_positions
    events = sorted([(t.entry_ts, 1) for t in res.trades] + [(t.exit_ts, -1) for t in res.trades])
    open_now, peak = 0, 0
    for _, d in events:
        open_now += d
        peak = max(peak, open_now)
    assert peak <= CONFIG.risk.max_open_positions
    assert all(t.confidence >= CONFIG.risk.confidence_threshold for t in res.trades)


def test_summary_and_monte_carlo(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    _, _, res = synthetic_run
    rep = summarize(res)
    s = rep["summary"]
    assert s["trades"] == len(res.trades)
    assert 0 <= s["win_rate"] <= 100
    assert s["max_drawdown_pct"] >= 0
    assert set(rep["by_symbol"]) <= {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert sum(b["trades"] for b in rep["calibration"]) == len(res.trades)
    mc = monte_carlo_drawdown([t.r_multiple for t in res.trades], 1.0, runs=200)
    assert mc["dd_median_pct"] <= mc["dd_p95_pct"] <= mc["dd_max_pct"]


def test_walk_forward_uses_only_oos(synthetic_run) -> None:  # type: ignore[no-untyped-def]
    bt, prepared, _ = synthetic_run
    month = 30 * 86_400_000
    wf = walk_forward(
        bt,
        prepared,
        thresholds=(60, 70, 80),
        in_sample_ms=2 * month,
        out_of_sample_ms=month,
        min_trades=3,
    )
    assert wf.windows
    for w in wf.windows:
        assert all(w.oos_start <= t.entry_ts <= w.oos_end for t in w.oos_trades)
        assert w.best_threshold in (60, 70, 80)
    assert wf.summary["trades"] == len(wf.oos_trades)


def test_no_edge_on_random_walk() -> None:
    """Защита от заглядывания в будущее: на случайном блуждании без тренда у системы
    не может быть преимущества — ожидание должно быть около нуля или ниже (издержки)."""
    data = []
    for k in range(6):
        sym = f"RW{k}"
        h1 = make_ohlcv(6000, drift=0.0, vol=0.008, seed=100 + k, start_ts=T0)
        data.append(
            SymbolData(
                sym,
                replace(INST, symbol=sym, taker_fee=Decimal("0.00055")),
                h1,
                resample(h1, Timeframe.H1, Timeframe.H4),
            )
        )
    bt = Backtester(CONFIG, MARKET, BacktestSettings())
    res = bt.run(bt.prepare(data))
    s = summarize(res)["summary"]
    assert s["trades"] >= 50
    assert s["expectancy_r"] < 0.1
    assert isinstance(s["total_return_pct"], float)
