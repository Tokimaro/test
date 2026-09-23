from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain import Direction, Instrument, MarketType
from app.risk.correlation import correlation_matrix, correlations_for
from app.risk.manager import RiskEvent, RiskManager
from app.risk.sizing import size_position
from app.trading_config import RiskProfile, RiskSettings, resolve_risk

DAY = 86_400_000
T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC (среда)


def inst(step: str = "0.001", min_qty: str = "0.001", max_lev: int = 100) -> Instrument:
    return Instrument(
        symbol="BTCUSDT",
        market_type=MarketType.CRYPTO,
        category="linear",
        tick_size=Decimal("0.1"),
        qty_step=Decimal(step),
        min_qty=Decimal(min_qty),
        max_qty=Decimal(1000),
        min_notional=Decimal(5),
        max_leverage=Decimal(max_lev),
        taker_fee=Decimal("0.00055"),
    )


class TestSizing:
    def test_basic_long(self) -> None:
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=1.0,
            direction=Direction.LONG,
            entry=Decimal(60_000),
            stop=Decimal(59_000),
            instrument=inst(),
            available_margin=Decimal(10_000),
            max_leverage=Decimal(5),
        )
        assert r.ok
        # 100$ / (1000 + издержки ~126) ≈ 0.088
        assert r.qty == Decimal("0.088")
        assert r.risk_amount <= Decimal(100)
        assert r.leverage == 1  # 5280$ позиции помещаются в 10k без плеча

    def test_uses_minimal_leverage(self) -> None:
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=1.0,
            direction=Direction.LONG,
            entry=Decimal(100),
            stop=Decimal(99),
            instrument=inst(),
            available_margin=Decimal(3_000),
            max_leverage=Decimal(5),
        )
        assert r.ok
        # ~83 единицы × 100 = 8.3k → нужно плечо 3 при 3k свободной маржи
        assert r.leverage == 3

    def test_margin_cap_reduces_qty(self) -> None:
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=1.0,
            direction=Direction.LONG,
            entry=Decimal(100),
            stop=Decimal("99.9"),
            instrument=inst(),
            available_margin=Decimal(1_000),
            max_leverage=Decimal(5),
        )
        assert r.ok
        assert r.notional <= Decimal(5_000)
        assert r.risk_amount < Decimal(100)

    @pytest.mark.parametrize(
        ("direction", "stop", "reason"),
        [(Direction.LONG, 101, "stop_on_wrong_side"), (Direction.SHORT, 99, "stop_on_wrong_side")],
    )
    def test_rejects_wrong_stop(self, direction: Direction, stop: int, reason: str) -> None:
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=1.0,
            direction=direction,
            entry=Decimal(100),
            stop=Decimal(stop),
            instrument=inst(),
            available_margin=Decimal(10_000),
            max_leverage=Decimal(5),
        )
        assert r.reject == reason

    def test_below_min_qty(self) -> None:
        r = size_position(
            equity=Decimal(50),
            risk_pct=0.5,
            direction=Direction.LONG,
            entry=Decimal(60_000),
            stop=Decimal(58_000),
            instrument=inst(),
            available_margin=Decimal(50),
            max_leverage=Decimal(5),
        )
        assert r.reject == "below_min_qty"

    def test_liquidation_too_close(self) -> None:
        # плечо 10 → ликвидация ~9.5% от входа, стоп 9% + ATR 1% → слишком близко
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=3.0,
            direction=Direction.LONG,
            entry=Decimal(100),
            stop=Decimal(91),
            instrument=inst(),
            available_margin=Decimal(330),
            max_leverage=Decimal(10),
            atr=Decimal(1),
        )
        assert r.reject == "liquidation_too_close"

    def test_spot_never_leveraged(self) -> None:
        r = size_position(
            equity=Decimal(10_000),
            risk_pct=1.0,
            direction=Direction.LONG,
            entry=Decimal(100),
            stop=Decimal(99),
            instrument=inst(),
            available_margin=Decimal(2_000),
            max_leverage=Decimal(5),
            derivatives=False,
        )
        assert r.leverage == 1
        assert r.notional <= Decimal(2_000)

    @settings(max_examples=300, deadline=None)
    @given(
        equity=st.decimals(min_value=100, max_value=10_000_000, places=2),
        risk_pct=st.floats(min_value=0.1, max_value=3.0),
        entry=st.decimals(min_value=Decimal("0.01"), max_value=100_000, places=2),
        stop_frac=st.floats(min_value=0.001, max_value=0.3),
        long=st.booleans(),
        margin_frac=st.floats(min_value=0.01, max_value=1.0),
        max_lev=st.integers(min_value=1, max_value=20),
        step=st.sampled_from(["1", "0.1", "0.001", "0.000001"]),
    )
    def test_invariants(
        self,
        equity: Decimal,
        risk_pct: float,
        entry: Decimal,
        stop_frac: float,
        long: bool,
        margin_frac: float,
        max_lev: int,
        step: str,
    ) -> None:
        direction = Direction.LONG if long else Direction.SHORT
        stop = entry * (1 - direction.sign * Decimal(str(stop_frac)))
        available = equity * Decimal(str(margin_frac))
        instrument = inst(step=step, min_qty=step)
        r = size_position(
            equity=equity,
            risk_pct=risk_pct,
            direction=direction,
            entry=entry,
            stop=stop,
            instrument=instrument,
            available_margin=available,
            max_leverage=Decimal(max_lev),
        )
        if not r.ok:
            return
        budget = equity * Decimal(str(risk_pct)) / 100
        # риск с издержками никогда не превышает бюджет
        assert r.risk_amount <= budget
        # объём кратен шагу и не меньше минимального
        assert r.qty % instrument.qty_step == 0
        assert r.qty >= instrument.min_qty
        # позиция помещается в маржу
        assert r.notional <= available * r.leverage
        assert 1 <= r.leverage <= max_lev


def mgr(**kw: object) -> tuple[RiskManager, list[RiskEvent]]:
    events: list[RiskEvent] = []
    settings = RiskSettings(profile=RiskProfile.CUSTOM, **kw)
    m = RiskManager(settings, on_event=events.append)
    m.on_equity(T0, 10_000)
    return m, events


class TestRiskManager:
    def test_allows_and_uses_base_risk(self) -> None:
        m, _ = mgr()
        d = m.check_new_trade(T0, "BTCUSDT", Direction.LONG, 70)
        assert d.allowed and d.risk_pct == 1.0

    def test_threshold(self) -> None:
        m, _ = mgr()
        assert m.check_new_trade(T0, "X", Direction.LONG, 60).reason == "below_threshold"

    def test_max_positions_and_duplicates(self) -> None:
        m, _ = mgr(max_open_positions=2, max_total_open_risk_pct=10)
        m.on_position_opened("A", Direction.LONG, 100)
        assert m.check_new_trade(T0, "A", Direction.LONG, 90).reason == "position_exists"
        m.on_position_opened("B", Direction.LONG, 100)
        assert m.check_new_trade(T0, "C", Direction.LONG, 90).reason == "max_open_positions"
        m.on_position_closed("B")
        assert m.check_new_trade(T0, "C", Direction.LONG, 90).allowed

    def test_total_open_risk_and_breakeven_frees_it(self) -> None:
        m, _ = mgr(max_total_open_risk_pct=2.5)
        m.on_position_opened("A", Direction.LONG, 100)
        m.on_position_opened("B", Direction.SHORT, 100)
        assert m.check_new_trade(T0, "C", Direction.LONG, 90).reason == "max_total_open_risk"
        m.update_open_risk("A", 0)  # позиция A переведена в безубыток
        assert m.check_new_trade(T0, "C", Direction.LONG, 90).allowed

    def test_daily_limit_resets_next_day_and_emits_once(self) -> None:
        m, events = mgr(daily_loss_limit_pct=3, weekly_loss_limit_pct=10)
        m.on_equity(T0 + 3_600_000, 9_650)
        assert m.check_new_trade(T0 + 3_600_000, "X", Direction.LONG, 90).reason == (
            "daily_loss_limit"
        )
        m.check_new_trade(T0 + 3_700_000, "X", Direction.LONG, 90)
        assert [e.type for e in events].count("daily_limit") == 1
        m.on_equity(T0 + DAY, 9_650)  # новый день — новая точка отсчёта
        assert m.check_new_trade(T0 + DAY, "X", Direction.LONG, 90).allowed

    def test_weekly_limit(self) -> None:
        m, _ = mgr(daily_loss_limit_pct=3, weekly_loss_limit_pct=6)
        # T0 — среда; чт, пт, сб — та же ISO-неделя
        for day, eq in enumerate([9_800, 9_650, 9_500]):
            m.on_equity(T0 + (day + 1) * DAY, eq)
        assert m.check_new_trade(T0 + 3 * DAY, "X", Direction.LONG, 90).allowed  # -5% за неделю
        m.on_equity(T0 + 4 * DAY, 9_350)  # воскресенье той же недели: -6.5%
        assert m.check_new_trade(T0 + 4 * DAY, "X", Direction.LONG, 90).reason == (
            "weekly_loss_limit"
        )

    def test_losing_streak_halves_risk_until_win(self) -> None:
        m, events = mgr(losing_streak_cut=3)
        for _ in range(3):
            m.on_trade_closed(T0, -50)
        assert m.current_risk_pct() == 0.5
        assert any(e.type == "risk_reduced" for e in events)
        m.on_trade_closed(T0, 80)
        assert m.current_risk_pct() == 1.0

    def test_drawdown_reduces_then_halts(self) -> None:
        m, events = mgr(max_drawdown_stop_pct=15, daily_loss_limit_pct=20, weekly_loss_limit_pct=40)
        m.on_equity(T0, 12_000)
        m.on_equity(T0 + 1, 11_000)  # просадка 8.3% > 7.5% → риск ×0.5
        assert m.current_risk_pct() == 0.5
        m.on_equity(T0 + 2, 10_100)  # 15.8% → остановка
        assert m.state.halted
        assert m.check_new_trade(T0 + 2, "X", Direction.LONG, 99).reason == "halted:max_drawdown"
        assert events[-1].type == "max_drawdown"
        m.resume()
        assert m.check_new_trade(T0 + 3, "X", Direction.LONG, 99).allowed

    def test_scale_by_confidence(self) -> None:
        m, _ = mgr(scale_by_confidence=True, confidence_threshold=60)
        assert m.current_risk_pct(100) == 1.0
        assert m.current_risk_pct(80) == 0.5
        assert m.current_risk_pct(61) == 0.5  # нижняя граница 0.5

    def test_correlated_exposure(self) -> None:
        m, _ = mgr(max_correlated_positions=1, max_total_open_risk_pct=10)
        m.on_position_opened("ETHUSDT", Direction.LONG, 50)
        corr = {"ETHUSDT": 0.9}
        assert m.check_new_trade(T0, "SOLUSDT", Direction.LONG, 90, corr).reason == (
            "correlated_exposure"
        )
        # противоположное направление не увеличивает экспозицию
        assert m.check_new_trade(T0, "SOLUSDT", Direction.SHORT, 90, corr).allowed

    def test_state_roundtrip(self) -> None:
        m, _ = mgr()
        m.on_position_opened("A", Direction.SHORT, 42.0)
        m.on_trade_closed(T0, -1)
        restored = RiskManager(m.settings, state=RiskManager.state_from_dict(m.to_dict()))
        assert restored.state == m.state


def test_profiles_are_internally_consistent() -> None:
    for p in RiskProfile:
        if p is not RiskProfile.CUSTOM:
            r = resolve_risk({"profile": p.value})
            assert r.risk_per_trade_pct <= r.max_total_open_risk_pct


def test_correlation_matrix() -> None:
    rng = np.random.default_rng(0)
    base = np.cumsum(rng.normal(0, 0.01, 300))
    idx = pd.RangeIndex(300)
    closes = {
        "A": pd.Series(100 * np.exp(base), index=idx),
        "B": pd.Series(50 * np.exp(base + rng.normal(0, 0.001, 300)), index=idx),
        "C": pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))), index=idx),
    }
    corr = correlations_for(correlation_matrix(closes), "A")
    assert corr["B"] > 0.9
    assert abs(corr["C"]) < 0.3
    assert "A" not in corr
