import numpy as np
import pandas as pd
import pytest

from app.research import strategies as research
from app.strategy.trend import latest_signals, target_weights
from app.trading_config import RiskProfile, RiskSettings, StrategySettings

DAY = 86_400_000
CFG = StrategySettings()
RISK = RiskSettings()


def prices(n: int = 500, cols: int = 6, seed: int = 0, drift: float = 0.0005) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = 100 * np.exp(np.cumsum(rng.normal(drift, 0.03, (n, cols)), axis=0))
    return pd.DataFrame(
        data,
        index=pd.Index(np.arange(n, dtype="int64") * DAY),
        columns=["BTCUSDT", *[f"C{i}USDT" for i in range(cols - 1)]],
    )


def test_matches_research_tsmom() -> None:
    """Бот торгует ровно то, что проверялось: доли совпадают с исследовательским кодом
    (TSMOM только лонг, ежедневный пересчёт; ребалансировку по дням делает движок)."""
    c = prices()
    c.iloc[:150, 3] = np.nan  # монета появилась позже остальных
    expected = research.tsmom(c, 365, long_only=True, rebalance=1)
    got, _ = target_weights(c, CFG, RISK)
    pd.testing.assert_frame_equal(got, expected, check_exact=False, atol=1e-12)


def test_weights_are_causal() -> None:
    c = prices()
    full, _ = target_weights(c, CFG, RISK)
    for cut in (200, 377):
        part, _ = target_weights(c.iloc[:cut], CFG, RISK)
        pd.testing.assert_frame_equal(full.iloc[:cut], part, check_exact=False, atol=1e-12)


def test_long_only_no_leverage() -> None:
    c = prices(drift=0.004)  # сильный рост — желание взять больше 100%
    w, _ = target_weights(c, CFG, RiskSettings(profile=RiskProfile.CUSTOM, target_vol_pct=100))
    assert (w >= 0).all().all()
    assert (w.sum(axis=1) <= 1 + 1e-9).all()
    assert w.sum(axis=1).iloc[-1] == pytest.approx(1.0)


def test_downtrend_goes_to_cash() -> None:
    c = prices(drift=-0.01, seed=3)
    w, sig = target_weights(c, CFG, RISK)
    assert w.iloc[-1].sum() == 0
    assert (sig.iloc[-1] == 0).all()


def test_no_weight_before_enough_history() -> None:
    w, _ = target_weights(prices(n=121), CFG, RISK)
    assert (w.iloc[:120] == 0).all().all()


def test_max_weight_caps_single_coin() -> None:
    c = prices(drift=0.004)
    risk = RiskSettings(profile=RiskProfile.CUSTOM, target_vol_pct=100, max_weight_pct=10)
    w, _ = target_weights(c, CFG, risk)
    assert w.max().max() <= 0.1 + 1e-12


def test_btc_filter_zeroes_weights_below_ma() -> None:
    c = prices(drift=0.003)
    c["BTCUSDT"] = np.linspace(200, 50, len(c))  # BTC всё время падает
    cfg = StrategySettings(btc_filter=True)
    w, _ = target_weights(c, cfg, RISK)
    assert w.iloc[-1].sum() == 0
    w_off, _ = target_weights(c, CFG, RISK)
    assert w_off.iloc[-1].sum() > 0


def test_latest_signals_components() -> None:
    c = prices(drift=0.003)
    sigs = latest_signals(c, list(c.columns), CFG, RISK)
    assert [s.symbol for s in sigs] == list(c.columns)
    s = sigs[1]
    assert s.ts == int(c.index[-1])
    assert set(s.components) >= {"ret_20d", "ret_60d", "ret_120d", "vol_pct", "history_days"}
    assert s.direction == ("long" if s.weight > 0 else None)
    assert 0 <= s.score <= 1
