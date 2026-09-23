"""План сделки: стоп-лосс и тейк-профиты (раздел 5.6 плана)."""

from dataclasses import dataclass

from app.domain import Direction
from app.strategy.base import Row, isnan
from app.strategy.ensemble import Signal
from app.trading_config import StopSettings

SWING_BUFFER_ATR = 0.1  # стоп ставится чуть дальше свинга


@dataclass(frozen=True, slots=True)
class TradePlan:
    direction: Direction
    entry: float  # ожидаемая цена входа (закрытие сигнальной свечи)
    stop: float
    tp1: float | None  # частичная фиксация (None — нет частичного выхода)
    tp2: float  # основная цель
    tp1_fraction: float  # доля позиции, закрываемая на tp1 (0..1)
    trailing: bool  # вести остаток трейлинг-стопом по Chandelier
    reward_risk: float  # средневзвешенное R:R
    atr: float
    strategy: str

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


@dataclass(frozen=True, slots=True)
class PlanRejected:
    reason: str


def plan_trade(signal: Signal, row: Row, cfg: StopSettings) -> TradePlan | PlanRejected:
    if signal.direction is None:
        return PlanRejected("no_direction")
    entry, atr = row["close"], row["atr"]
    if isnan(entry, atr) or atr <= 0 or entry <= 0:
        return PlanRejected("no_atr")
    sign = signal.direction.sign

    # SL: за свингом, но в коридоре [sl_atr_min; sl_atr_max] × ATR
    swing = row["swing_low"] if sign > 0 else row["swing_high"]
    min_d, max_d = cfg.sl_atr_min * atr, cfg.sl_atr_max * atr
    if not isnan(swing) and (entry - swing) * sign > 0:
        dist = abs(entry - swing) + SWING_BUFFER_ATR * atr
        dist = min(max(dist, min_d), max_d)
    else:
        dist = min_d
    stop = entry - sign * dist

    if signal.strategy == "mean_reversion":
        target = row["bb_mid"]
        if isnan(target) or (target - entry) * sign <= 0:
            return PlanRejected("no_mean_target")
        rr = abs(target - entry) / dist
        if rr < cfg.min_rr_mean_reversion:
            return PlanRejected(f"rr_too_low:{rr:.2f}")
        return TradePlan(
            direction=signal.direction,
            entry=entry,
            stop=stop,
            tp1=None,
            tp2=target,
            tp1_fraction=0.0,
            trailing=False,
            reward_risk=rr,
            atr=atr,
            strategy=signal.strategy,
        )

    frac = cfg.tp1_close_pct / 100.0
    rr = frac * cfg.tp1_r + (1 - frac) * cfg.tp2_r
    if rr < cfg.min_rr:
        return PlanRejected(f"rr_too_low:{rr:.2f}")
    return TradePlan(
        direction=signal.direction,
        entry=entry,
        stop=stop,
        tp1=entry + sign * cfg.tp1_r * dist if frac > 0 else None,
        tp2=entry + sign * cfg.tp2_r * dist,
        tp1_fraction=frac,
        trailing=True,
        reward_risk=rr,
        atr=atr,
        strategy=signal.strategy or "trend",
    )
