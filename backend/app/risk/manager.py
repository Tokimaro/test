"""Риск портфеля: пик капитала, просадка и остановка по просадке.

Размер позиций задаёт сама стратегия (целевая волатильность, доля ≤ 100% капитала). Здесь —
защитный предохранитель: при просадке глубже max_drawdown_stop_pct бот продаёт всё в USDT и
останавливается до ручного возобновления. Состояние сериализуется, чтобы переживать рестарт.
Время передаётся явно (мс UTC) — один и тот же код работает в бэктесте и live.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from app.trading_config import RiskSettings


@dataclass(frozen=True, slots=True)
class RiskEvent:
    ts: int
    type: str  # drawdown_stop / kill_switch / ...
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskState:
    equity_peak: float = 0.0
    last_equity: float = 0.0
    halted: bool = False
    halt_reason: str | None = None


class RiskManager:
    def __init__(
        self, settings: RiskSettings, on_event: Callable[[RiskEvent], None] | None = None
    ) -> None:
        self.settings = settings
        self.state = RiskState()
        self._on_event = on_event

    def _emit(self, ts: int, type_: str, **details: Any) -> None:
        if self._on_event is not None:
            self._on_event(RiskEvent(ts, type_, details))

    def on_equity(self, ts: int, equity: float) -> bool:
        """Учитывает капитал. True — только что сработала остановка по просадке."""
        s = self.state
        s.last_equity = equity
        s.equity_peak = max(s.equity_peak, equity)
        limit = self.settings.max_drawdown_stop_pct
        if limit > 0 and not s.halted and self.drawdown_pct() >= limit:
            self.halt(ts, "max_drawdown", drawdown_pct=round(self.drawdown_pct(), 2))
            return True
        return False

    def drawdown_pct(self) -> float:
        s = self.state
        if s.equity_peak <= 0:
            return 0.0
        return max(0.0, (s.equity_peak - s.last_equity) / s.equity_peak * 100)

    def halt(self, ts: int, reason: str, **details: Any) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self._emit(ts, "drawdown_stop" if reason == "max_drawdown" else reason, **details)

    def resume(self) -> None:
        """Ручное возобновление: пик капитала сбрасывается к текущему значению, иначе
        остановка по просадке сработала бы снова сразу же."""
        self.state.halted = False
        self.state.halt_reason = None
        self.state.equity_peak = self.state.last_equity

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.state)

    @staticmethod
    def state_from_dict(d: dict[str, Any]) -> RiskState:
        known = RiskState.__dataclass_fields__
        return RiskState(**{k: v for k, v in d.items() if k in known})
