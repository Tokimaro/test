"""Логика ведения открытой позиции (раздел 5.6 плана), общая для бэктеста и live.

На закрытии каждой свечи: тайм-стоп, трейлинг-стоп по Chandelier. После TP1 — перенос стопа
в безубыток (с учётом комиссий). Стоп двигается только в сторону прибыли.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.domain import Direction
from app.trading_config import StopSettings


class CloseReason(StrEnum):
    STOP = "sl"
    BREAKEVEN = "be"
    TRAILING = "trailing"
    TP1 = "tp1"
    TP2 = "tp2"
    TIME = "time"
    MANUAL = "manual"
    KILL = "kill"
    END = "end"  # конец данных бэктеста


class StopKind(StrEnum):
    INITIAL = "initial"
    BREAKEVEN = "breakeven"
    TRAILING = "trailing"

    @property
    def close_reason(self) -> CloseReason:
        return {
            StopKind.INITIAL: CloseReason.STOP,
            StopKind.BREAKEVEN: CloseReason.BREAKEVEN,
            StopKind.TRAILING: CloseReason.TRAILING,
        }[self]


@dataclass
class ManagedPosition:
    symbol: str
    direction: Direction
    strategy: str
    entry: float
    qty: float
    initial_stop: float
    stop: float
    tp1: float | None
    tp2: float
    tp1_fraction: float
    trailing: bool
    opened_ts: int
    risk_amount: float
    confidence: float = 0.0
    regime: str = ""
    remaining: float = 0.0
    tp1_done: bool = False
    stop_kind: StopKind = StopKind.INITIAL
    bars_held: int = 0
    realized_pnl: float = 0.0  # с учётом комиссий и funding
    fees: float = 0.0
    funding: float = 0.0
    exit_value: float = 0.0  # Σ цена × объём выходов — для средней цены выхода
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.remaining == 0.0:
            self.remaining = self.qty

    @property
    def sign(self) -> int:
        return self.direction.sign

    def unrealized(self, price: float) -> float:
        return (price - self.entry) * self.sign * self.remaining

    def open_risk(self) -> float:
        """Сколько потеряем при срабатывании текущего стопа (0, если стоп в плюсе)."""
        return max(0.0, (self.entry - self.stop) * self.sign * self.remaining)

    def trailing_active(self) -> bool:
        return self.trailing and (self.tp1 is None or self.tp1_done)


@dataclass(frozen=True, slots=True)
class MoveStop:
    price: float
    kind: StopKind


@dataclass(frozen=True, slots=True)
class ClosePosition:
    reason: CloseReason


Action = MoveStop | ClosePosition


def breakeven_price(pos: ManagedPosition, fee_rate: float) -> float:
    """Цена, при которой закрытие остатка покрывает комиссии входа и выхода."""
    return pos.entry * (1 + pos.sign * 2 * fee_rate)


def improves(pos: ManagedPosition, new_stop: float) -> bool:
    return (new_stop - pos.stop) * pos.sign > 0


def on_tp1_filled(pos: ManagedPosition, fee_rate: float) -> MoveStop | None:
    be = breakeven_price(pos, fee_rate)
    return MoveStop(be, StopKind.BREAKEVEN) if improves(pos, be) else None


def on_bar_close(
    pos: ManagedPosition,
    close: float,
    chandelier_long: float,
    chandelier_short: float,
    cfg: StopSettings,
) -> list[Action]:
    """Решения на закрытии свечи. Увеличивает bars_held."""
    pos.bars_held += 1
    # Тайм-стоп: сделка так и не дошла до первой цели
    first_target_reached = pos.tp1_done if pos.tp1 is not None else False
    if cfg.time_stop_bars and not first_target_reached and pos.bars_held >= cfg.time_stop_bars:
        return [ClosePosition(CloseReason.TIME)]

    if pos.trailing_active():
        candidate = chandelier_long if pos.sign > 0 else chandelier_short
        if (
            candidate == candidate
            and improves(pos, candidate)
            and (close - candidate) * pos.sign > 0
        ):
            return [MoveStop(candidate, StopKind.TRAILING)]
    return []


def apply_move(pos: ManagedPosition, move: MoveStop) -> None:
    if improves(pos, move.price):
        pos.stop = move.price
        pos.stop_kind = move.kind
