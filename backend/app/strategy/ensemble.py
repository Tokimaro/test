"""Ансамбль стратегий и расчёт уверенности (раздел 5.5 плана).

raw = Σ(w_i × score_i) / max(w)          — нормировка на вес главной стратегии режима,
                                           чтобы идеальный сетап основной стратегии
                                           давал 100%, а противоречащие — снижали оценку
raw × mtf_alignment × filters             — согласие старшего ТФ и внешние фильтры
confidence = |raw| × 100%, direction = sign(raw)
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.regime import Regime, classify_regime, higher_tf_bias
from app.domain import Direction
from app.strategy.base import MarketContext, Row, Strategy, SubSignal, clamp, columns_of
from app.strategy.breakout import BreakoutStrategy
from app.strategy.mean_reversion import MeanReversionStrategy
from app.strategy.trend import TrendStrategy
from app.trading_config import RegimeWeights, StrategySettings

# множитель при несогласии со старшим таймфреймом (раздел 5.5)
MTF_AGREE, MTF_NEUTRAL, MTF_AGAINST = 1.0, 0.5, 0.0

FUNDING_WARN = 0.0005  # 0.05% за 8ч
FUNDING_EXTREME = 0.001  # 0.1% за 8ч


@dataclass(frozen=True, slots=True)
class Signal:
    ts: int
    symbol: str
    direction: Direction | None
    confidence: float  # 0..100
    regime: Regime
    strategy: str  # стратегия с наибольшим вкладом в направлении сигнала
    components: dict[str, Any] = field(default_factory=dict)


def prepare(feats: pd.DataFrame, cfg: StrategySettings) -> pd.DataFrame:
    """Добавляет к признакам режим и направление старшего ТФ (векторно, один раз)."""
    out = feats.copy()
    out["regime"] = classify_regime(out, cfg)
    out["h_bias"] = higher_tf_bias(out)
    return out


class SignalEngine:
    def __init__(self, cfg: StrategySettings, strategies: list[Strategy] | None = None) -> None:
        self.cfg = cfg
        self.strategies = strategies or [
            TrendStrategy(),
            MeanReversionStrategy(),
            BreakoutStrategy(),
        ]

    def weights_for(self, regime: Regime) -> dict[str, float]:
        w = self.cfg.weights
        if regime.is_trend:
            return _as_dict(w.trend)
        if regime is Regime.RANGE:
            return _as_dict(w.range)
        # переходный режим — среднее трендовых и флэтовых весов
        t, r = _as_dict(w.trend), _as_dict(w.range)
        return {k: (t[k] + r[k]) / 2 for k in t}

    def evaluate_frame(
        self, prepared: pd.DataFrame, symbol: str, ctx: MarketContext | None = None
    ) -> list[Signal]:
        cols = columns_of(prepared)
        index = prepared.index.to_numpy()
        return [
            self.evaluate_row(Row(cols, i), int(index[i]), symbol, ctx)
            for i in range(len(prepared))
        ]

    def evaluate_last(
        self, prepared: pd.DataFrame, symbol: str, ctx: MarketContext | None = None
    ) -> Signal:
        cols = columns_of(prepared)
        i = len(prepared) - 1
        return self.evaluate_row(Row(cols, i), int(prepared.index[i]), symbol, ctx)

    def evaluate_row(
        self, row: Row, ts: int, symbol: str, ctx: MarketContext | None = None
    ) -> Signal:
        regime = Regime(row.get_str("regime"))
        if not regime.tradable:
            return Signal(ts, symbol, None, 0.0, regime, "", {"reject": f"regime_{regime}"})

        weights = self.weights_for(regime)
        subs = [s.evaluate(row) for s in self.strategies]
        weighted = {s.name: weights.get(s.name, 0.0) * s.score for s in subs}
        max_w = max(weights.values()) or 1.0
        raw = clamp(sum(weighted.values()) / max_w)

        bias = int(row["h_bias"])
        side = int(np.sign(raw))
        if side == 0:
            mtf = MTF_NEUTRAL
        elif bias == side:
            mtf = MTF_AGREE
        elif bias == 0:
            mtf = MTF_NEUTRAL
        else:
            mtf = MTF_AGAINST

        filters = self._filters(side, ctx)
        filt_mult = float(np.prod(list(filters.values()))) if filters else 1.0
        final = raw * mtf * filt_mult

        direction = None
        if final > 0:
            direction = Direction.LONG
        elif final < 0:
            direction = Direction.SHORT
        dominant = _dominant(subs, weighted, side)
        components = {
            "raw": round(raw, 4),
            "mtf": mtf,
            "h_bias": bias,
            "filters": filters,
            "weights": weights,
            "scores": {s.name: round(s.score, 4) for s in subs},
            "reasons": {s.name: s.reasons for s in subs},
        }
        return Signal(
            ts, symbol, direction, round(abs(final) * 100, 2), regime, dominant, components
        )

    @staticmethod
    def _filters(side: int, ctx: MarketContext | None) -> dict[str, float]:
        if ctx is None or side == 0:
            return {}
        out: dict[str, float] = {}
        if ctx.funding_rate is not None:
            # экстремальный funding в сторону сделки = перегретая толпа → штраф
            f = ctx.funding_rate * side
            if f > FUNDING_EXTREME:
                out["funding"] = 0.4
            elif f > FUNDING_WARN:
                out["funding"] = 0.7
        if not ctx.is_btc and ctx.btc_regime is not None:
            against = Regime.TREND_DOWN if side > 0 else Regime.TREND_UP
            if ctx.btc_regime is against:
                out["btc"] = 0.6
            elif ctx.btc_regime is Regime.CHAOS:
                out["btc"] = 0.5
        return out


def _as_dict(w: RegimeWeights) -> dict[str, float]:
    return {"trend": w.trend, "mean_reversion": w.mean_reversion, "breakout": w.breakout}


def _dominant(subs: list[SubSignal], weighted: dict[str, float], side: int) -> str:
    if side == 0:
        return ""
    best = max(subs, key=lambda s: weighted[s.name] * side)
    return best.name if weighted[best.name] * side > 0 else ""
