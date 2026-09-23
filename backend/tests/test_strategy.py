from typing import Any

import numpy as np
import pytest

from app.analysis.features import build_features
from app.analysis.regime import Regime
from app.domain import Direction, Timeframe
from app.strategy.base import MarketContext, Row
from app.strategy.breakout import BreakoutStrategy
from app.strategy.ensemble import Signal, SignalEngine, prepare
from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.planner import PlanRejected, TradePlan, plan_trade
from app.strategy.trend import TrendStrategy
from app.trading_config import StopSettings, StrategySettings
from tests.synthetic import make_ohlcv, resample

NAN = float("nan")

BASE: dict[str, Any] = {
    "open": 100.0,
    "high": 101.0,
    "low": 99.0,
    "close": 100.0,
    "volume": 100.0,
    "vol_sma": 100.0,
    "ema21": 100.0,
    "ema55": 100.0,
    "ema21_slope": 0.0,
    "ema55_slope": 0.0,
    "vwap": NAN,
    "atr": 2.0,
    "rsi": 50.0,
    "macd_hist": 0.0,
    "bb_upper": 110.0,
    "bb_lower": 90.0,
    "bb_mid": 100.0,
    "bb_width_pct": 50.0,
    "don_upper": 105.0,
    "don_lower": 95.0,
    "swing_low": NAN,
    "swing_high": NAN,
    "rsi_at_swing_low": NAN,
    "rsi_at_swing_high": NAN,
    "e_close": NAN,
    "e_open": NAN,
    "e_ema21": NAN,
    "regime": "trend_up",
    "h_bias": 1,
    "long_trend": 0,
}


def row(prev: dict[str, Any] | None = None, **cur: Any) -> Row:
    """Row из двух строк: предыдущей и текущей."""
    p = {**BASE, **(prev or {})}
    c = {**BASE, **cur}
    cols = {k: np.array([p[k], c[k]], dtype=object if k == "regime" else float) for k in BASE}
    return Row(cols, 1)


UPTREND = {
    "close": 104.0,
    "ema21": 103.0,
    "ema55": 100.0,
    "ema21_slope": 0.5,
    "ema55_slope": 0.2,
}


class TestTrend:
    def test_perfect_long_setup(self) -> None:
        r = row(
            prev={"macd_hist": 0.1, "low": 103.2},
            **UPTREND,
            low=102.9,  # коснулись EMA21
            macd_hist=0.3,
            volume=150.0,
            e_close=104.2,
            e_ema21=103.8,
        )
        s = TrendStrategy().evaluate(r)
        assert s.score == pytest.approx(1.0)

    def test_no_structure_no_signal(self) -> None:
        s = TrendStrategy().evaluate(row(ema21=101.0, ema55=100.0, ema21_slope=-0.1))
        assert s.score == 0.0

    def test_short_structure_with_opposing_entry_tf(self) -> None:
        r = row(
            close=96.0,
            ema21=97.0,
            ema55=100.0,
            ema21_slope=-0.5,
            ema55_slope=-0.2,
            high=97.2,
            e_close=97.5,
            e_ema21=97.0,  # младший ТФ против
        )
        s = TrendStrategy().evaluate(r)
        # структура + откат = 0.6 шорт, младший ТФ +0.1 лонг
        assert s.score == pytest.approx(-0.5)

    def test_warmup(self) -> None:
        assert TrendStrategy().evaluate(row(ema55=NAN)).reasons == {"skip": "warmup"}


class TestMeanReversion:
    def test_long_at_lower_band_with_divergence(self) -> None:
        r = row(
            low=89.0,
            close=91.0,
            rsi=22.0,
            swing_low=90.0,
            rsi_at_swing_low=18.0,
            e_close=91.5,
            e_open=90.5,
        )
        s = MeanReversionStrategy().evaluate(r)
        # band 0.35 + rsi 0.25*0.8 + div 0.25 + entry 0.15
        assert s.score == pytest.approx(0.35 + 0.2 + 0.25 + 0.15)

    def test_short_at_upper_band(self) -> None:
        s = MeanReversionStrategy().evaluate(row(high=111.0, rsi=85.0))
        assert s.score == pytest.approx(-(0.35 + 0.25))

    def test_inside_bands(self) -> None:
        assert MeanReversionStrategy().evaluate(row()).score == 0.0


class TestBreakout:
    def test_confirmed_breakout(self) -> None:
        r = row(close=106.0, volume=200.0, bb_width_pct=5.0)
        assert BreakoutStrategy().evaluate(r).score == pytest.approx(1.0)

    def test_unconfirmed_breakout_ignored(self) -> None:
        r = row(close=106.0, volume=100.0, bb_width_pct=50.0)
        assert BreakoutStrategy().evaluate(r).score == 0.0

    def test_breakdown_with_squeeze_in_recent_bars(self) -> None:
        r = row(prev={"bb_width_pct": 8.0}, close=94.0, bb_width_pct=30.0)
        assert BreakoutStrategy().evaluate(r).score == pytest.approx(-0.7)


class TestEnsemble:
    engine = SignalEngine(StrategySettings())

    def perfect_trend_row(self, **kw: Any) -> Row:
        return row(
            prev={"macd_hist": 0.1, "low": 103.2},
            **{
                **UPTREND,
                "low": 102.9,
                "macd_hist": 0.3,
                "volume": 150.0,
                "e_close": 104.2,
                "e_ema21": 103.8,
                **kw,
            },
        )

    def test_primary_strategy_alone_reaches_full_confidence(self) -> None:
        sig = self.engine.evaluate_row(self.perfect_trend_row(), 0, "BTCUSDT")
        assert sig.direction is Direction.LONG
        assert sig.confidence == pytest.approx(100.0)
        assert sig.strategy == "trend"

    def test_mtf_neutral_halves_and_against_kills(self) -> None:
        neutral = self.engine.evaluate_row(self.perfect_trend_row(h_bias=0), 0, "X")
        against = self.engine.evaluate_row(self.perfect_trend_row(h_bias=-1), 0, "X")
        assert neutral.confidence == pytest.approx(50.0)
        assert against.confidence == 0.0
        assert against.direction is None

    def test_range_regime_downweights_trend(self) -> None:
        sig = self.engine.evaluate_row(self.perfect_trend_row(regime="range"), 0, "X")
        # в режиме RANGE вес тренда 0.1, нормировка на 0.6
        assert sig.confidence == pytest.approx(100 * 0.1 / 0.6, abs=0.01)

    @pytest.mark.parametrize("regime", ["chaos", "unknown"])
    def test_untradable_regimes(self, regime: str) -> None:
        sig = self.engine.evaluate_row(self.perfect_trend_row(regime=regime), 0, "X")
        assert sig.direction is None
        assert sig.components["reject"] == f"regime_{regime}"

    def test_filters(self) -> None:
        ctx = MarketContext(funding_rate=0.002, btc_regime=Regime.TREND_DOWN)
        sig = self.engine.evaluate_row(self.perfect_trend_row(), 0, "ETHUSDT", ctx)
        assert sig.components["filters"] == {"funding": 0.4, "btc": 0.6}
        assert sig.confidence == pytest.approx(100 * 0.4 * 0.6)
        # для самого BTC фильтр по BTC не применяется
        own = MarketContext(btc_regime=Regime.TREND_DOWN, is_btc=True)
        assert (
            self.engine.evaluate_row(self.perfect_trend_row(), 0, "BTCUSDT", own).confidence == 100
        )

    def test_counter_trend_penalties(self) -> None:
        # лонг против падающего долгосрочного тренда монеты — уверенность ×0.75
        own = self.engine.evaluate_row(self.perfect_trend_row(long_trend=-1), 0, "ETHUSDT")
        assert own.components["filters"] == {"counter_trend": 0.75}
        assert own.confidence == pytest.approx(75.0)
        # по тренду — без штрафа
        assert (
            self.engine.evaluate_row(self.perfect_trend_row(long_trend=1), 0, "X").confidence == 100
        )
        # против тренда всего рынка (BTC) — ×0.85, к самому BTC не применяется
        ctx = MarketContext(market_trend=-1)
        mkt = self.engine.evaluate_row(self.perfect_trend_row(), 0, "ETHUSDT", ctx)
        assert mkt.components["filters"] == {"market_trend": 0.85}
        both = self.engine.evaluate_row(self.perfect_trend_row(long_trend=-1), 0, "SOL", ctx)
        assert both.confidence == pytest.approx(100 * 0.75 * 0.85)
        btc = MarketContext(market_trend=-1, is_btc=True)
        assert (
            self.engine.evaluate_row(self.perfect_trend_row(), 0, "BTCUSDT", btc).confidence == 100
        )

    def test_transition_uses_blended_weights(self) -> None:
        w = self.engine.weights_for(Regime.TRANSITION)
        assert w == pytest.approx({"trend": 0.35, "mean_reversion": 0.35, "breakout": 0.3})


def sig(direction: Direction, strategy: str = "trend") -> Signal:
    return Signal(0, "X", direction, 80.0, Regime.TREND_UP, strategy)


class TestPlanner:
    cfg = StopSettings()

    def test_trend_long_stop_clamped_to_min_atr(self) -> None:
        # свинг очень близко → стоп отодвигается до 1.5 ATR
        plan = plan_trade(sig(Direction.LONG), row(close=100.0, swing_low=99.5, atr=2.0), self.cfg)
        assert isinstance(plan, TradePlan)
        assert plan.stop == pytest.approx(97.0)
        assert plan.tp1 == pytest.approx(104.5)
        assert plan.tp2 == pytest.approx(109.0)
        assert plan.reward_risk == pytest.approx(2.25)
        assert plan.trailing

    def test_stop_behind_swing_and_capped(self) -> None:
        p1 = plan_trade(sig(Direction.SHORT), row(close=100.0, swing_high=104.0, atr=2.0), self.cfg)
        assert isinstance(p1, TradePlan)
        assert p1.stop == pytest.approx(104.2)  # свинг + 0.1 ATR
        assert p1.tp2 < p1.entry < p1.stop
        p2 = plan_trade(sig(Direction.SHORT), row(close=100.0, swing_high=120.0, atr=2.0), self.cfg)
        assert isinstance(p2, TradePlan)
        assert p2.stop == pytest.approx(106.0)  # не дальше 3 ATR

    def test_swing_on_wrong_side_ignored(self) -> None:
        plan = plan_trade(sig(Direction.LONG), row(close=100.0, swing_low=101.0), self.cfg)
        assert isinstance(plan, TradePlan)
        assert plan.stop == pytest.approx(97.0)

    def test_mean_reversion_target_and_rr(self) -> None:
        good = plan_trade(
            sig(Direction.LONG, "mean_reversion"), row(close=90.0, bb_mid=100.0, atr=2.0), self.cfg
        )
        assert isinstance(good, TradePlan)
        assert good.tp2 == 100.0 and good.tp1 is None and not good.trailing
        assert good.reward_risk == pytest.approx(10 / 3)
        bad = plan_trade(
            sig(Direction.LONG, "mean_reversion"), row(close=98.0, bb_mid=100.0, atr=2.0), self.cfg
        )
        assert isinstance(bad, PlanRejected)
        assert bad.reason.startswith("rr_too_low")

    def test_rr_config_too_low_rejects(self) -> None:
        cfg = StopSettings(tp1_r=1.0, tp2_r=1.5, tp1_close_pct=50, min_rr=2.0)
        assert isinstance(plan_trade(sig(Direction.LONG), row(), cfg), PlanRejected)


def test_signals_follow_synthetic_trends() -> None:
    """Интеграция: в сильном росте сигналы лонговые, в падении — шортовые."""
    cfg = StrategySettings()
    n = 3000
    drifts = np.zeros(n)
    drifts[1000:1700] = 0.003
    drifts[1700:2400] = -0.003
    df = make_ohlcv(n, drifts=drifts, vol=0.006, seed=5)
    h4 = resample(df, Timeframe.H1, Timeframe.H4)
    feats = prepare(build_features(df, Timeframe.H1, h4, Timeframe.H4, None, None, cfg), cfg)
    signals = SignalEngine(cfg).evaluate_frame(feats, "SYN")
    strong = [(i, s) for i, s in enumerate(signals) if s.confidence >= 65]
    assert strong, "должны быть сильные сигналы"
    up = [s.direction for i, s in strong if 1200 <= i < 1700]
    down = [s.direction for i, s in strong if 1900 <= i < 2400]
    assert up and up.count(Direction.LONG) / len(up) > 0.9
    assert down and down.count(Direction.SHORT) / len(down) > 0.9
    assert all(s.confidence == 0 for s in signals[:50])  # прогрев
