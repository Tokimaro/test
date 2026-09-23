"""Риск-менеджер: лимиты и динамическая коррекция риска (разделы 6.2–6.4 плана).

Состояние сериализуется (to_dict/from_dict), чтобы переживать рестарт бота.
Время передаётся явно (мс UTC) — один и тот же код работает в бэктесте и live.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.domain import Direction
from app.trading_config import RiskSettings


@dataclass(frozen=True, slots=True)
class RiskEvent:
    ts: int
    type: str  # daily_limit / weekly_limit / drawdown_stop / risk_reduced / kill_switch / ...
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    risk_pct: float = 0.0
    reason: str | None = None


@dataclass
class OpenRisk:
    direction: Direction
    risk_amount: float  # сколько потеряем, если сработает текущий стоп (0 после безубытка)


@dataclass
class RiskState:
    equity_peak: float = 0.0
    day_key: str = ""
    day_start_equity: float = 0.0
    week_key: str = ""
    week_start_equity: float = 0.0
    last_equity: float = 0.0
    losing_streak: int = 0
    halted: bool = False
    halt_reason: str | None = None
    open: dict[str, OpenRisk] = field(default_factory=dict)
    # последний период, за который уже отправлено событие лимита (чтобы не спамить)
    notified: dict[str, str] = field(default_factory=dict)


def _day_key(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def _week_key(ts: int) -> str:
    y, w, _ = datetime.fromtimestamp(ts / 1000, tz=UTC).isocalendar()
    return f"{y}-W{w:02d}"


class RiskManager:
    def __init__(
        self,
        settings: RiskSettings,
        on_event: Callable[[RiskEvent], None] | None = None,
        state: RiskState | None = None,
    ) -> None:
        self.settings = settings
        self.state = state or RiskState()
        self._on_event = on_event

    # ------------------------------------------------------------------ события
    def _emit(self, ts: int, type_: str, **details: Any) -> None:
        if self._on_event is not None:
            self._on_event(RiskEvent(ts, type_, details))

    def _emit_once(self, ts: int, type_: str, period: str, **details: Any) -> None:
        if self.state.notified.get(type_) != period:
            self.state.notified[type_] = period
            self._emit(ts, type_, **details)

    def on_equity(self, ts: int, equity: float) -> None:
        """Вызывается при каждом обновлении капитала (закрытие свечи, сделки)."""
        s = self.state
        s.last_equity = equity
        if equity > s.equity_peak:
            s.equity_peak = equity
        day, week = _day_key(ts), _week_key(ts)
        if day != s.day_key:
            s.day_key, s.day_start_equity = day, equity
        if week != s.week_key:
            s.week_key, s.week_start_equity = week, equity
        if not s.halted and self.drawdown_pct() >= self.settings.max_drawdown_stop_pct:
            self.halt(ts, "max_drawdown", drawdown_pct=round(self.drawdown_pct(), 2))

    def on_trade_closed(self, ts: int, pnl: float) -> None:
        s = self.state
        before = self.current_risk_pct()
        s.losing_streak = s.losing_streak + 1 if pnl < 0 else 0
        after = self.current_risk_pct()
        if after < before:
            self._emit(ts, "risk_reduced", risk_pct=after, losing_streak=s.losing_streak)

    def on_position_opened(self, symbol: str, direction: Direction, risk_amount: float) -> None:
        self.state.open[symbol] = OpenRisk(direction, max(0.0, risk_amount))

    def update_open_risk(self, symbol: str, risk_amount: float) -> None:
        if symbol in self.state.open:
            self.state.open[symbol].risk_amount = max(0.0, risk_amount)

    def on_position_closed(self, symbol: str) -> None:
        self.state.open.pop(symbol, None)

    def halt(self, ts: int, reason: str, **details: Any) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self._emit(ts, reason if reason != "manual" else "halted", **details)

    def resume(self) -> None:
        """Ручное снятие остановки (из панели). Пик капитала сбрасывается на текущий,
        иначе бот сразу остановится снова по той же просадке."""
        self.state.halted = False
        self.state.halt_reason = None
        self.state.equity_peak = self.state.last_equity

    # ------------------------------------------------------------------ метрики
    def drawdown_pct(self) -> float:
        s = self.state
        if s.equity_peak <= 0:
            return 0.0
        return max(0.0, (s.equity_peak - s.last_equity) / s.equity_peak * 100)

    def day_pnl_pct(self) -> float:
        s = self.state
        return (s.last_equity / s.day_start_equity - 1) * 100 if s.day_start_equity > 0 else 0.0

    def week_pnl_pct(self) -> float:
        s = self.state
        return (s.last_equity / s.week_start_equity - 1) * 100 if s.week_start_equity > 0 else 0.0

    def open_risk_pct(self) -> float:
        eq = self.state.last_equity
        total = sum(o.risk_amount for o in self.state.open.values())
        return total / eq * 100 if eq > 0 else 0.0

    def current_risk_pct(self, confidence: float | None = None) -> float:
        """Базовый риск с учётом серии убытков, просадки и (опционально) уверенности."""
        cfg = self.settings
        risk = cfg.risk_per_trade_pct
        if self.state.losing_streak >= cfg.losing_streak_cut:
            risk *= cfg.risk_reduction_factor
        if self.drawdown_pct() > cfg.max_drawdown_stop_pct / 2:
            risk *= cfg.risk_reduction_factor
        if cfg.scale_by_confidence and confidence is not None:
            thr = cfg.confidence_threshold
            k = (confidence - thr) / (100 - thr) if thr < 100 else 1.0
            risk *= min(1.0, max(0.5, k))
        return risk

    # ------------------------------------------------------------------ решение
    def check_new_trade(
        self,
        ts: int,
        symbol: str,
        direction: Direction,
        confidence: float,
        correlations: dict[str, float] | None = None,
    ) -> RiskDecision:
        cfg = self.settings
        s = self.state
        if s.halted:
            return RiskDecision(False, reason=f"halted:{s.halt_reason}")
        if confidence < cfg.confidence_threshold:
            return RiskDecision(False, reason="below_threshold")
        if self.day_pnl_pct() <= -cfg.daily_loss_limit_pct:
            self._emit_once(ts, "daily_limit", s.day_key, day_pnl_pct=round(self.day_pnl_pct(), 2))
            return RiskDecision(False, reason="daily_loss_limit")
        if self.week_pnl_pct() <= -cfg.weekly_loss_limit_pct:
            self._emit_once(
                ts, "weekly_limit", s.week_key, week_pnl_pct=round(self.week_pnl_pct(), 2)
            )
            return RiskDecision(False, reason="weekly_loss_limit")
        if symbol in s.open:
            return RiskDecision(False, reason="position_exists")
        if len(s.open) >= cfg.max_open_positions:
            return RiskDecision(False, reason="max_open_positions")

        risk = self.current_risk_pct(confidence)
        if self.open_risk_pct() + risk > cfg.max_total_open_risk_pct + 1e-9:
            return RiskDecision(False, reason="max_total_open_risk")

        if correlations:
            same_side_correlated = sum(
                1
                for other, pos in s.open.items()
                if pos.direction is direction
                and correlations.get(other, 0.0) >= cfg.correlation_threshold
            )
            if same_side_correlated >= cfg.max_correlated_positions:
                return RiskDecision(False, reason="correlated_exposure")

        return RiskDecision(True, risk_pct=risk)

    # ------------------------------------------------------------------ персистентность
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self.state)
        d["open"] = {
            k: {"direction": v.direction.value, "risk_amount": v.risk_amount}
            for k, v in self.state.open.items()
        }
        return d

    @staticmethod
    def state_from_dict(d: dict[str, Any]) -> RiskState:
        data = dict(d)
        data["open"] = {
            k: OpenRisk(Direction(v["direction"]), float(v["risk_amount"]))
            for k, v in d.get("open", {}).items()
        }
        return RiskState(**data)
