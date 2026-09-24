from app.risk.circuit import CircuitBreaker
from app.risk.manager import RiskEvent, RiskManager
from app.trading_config import RiskProfile, RiskSettings


def manager(stop: float = 20.0) -> tuple[RiskManager, list[RiskEvent]]:
    events: list[RiskEvent] = []
    settings = RiskSettings(profile=RiskProfile.CUSTOM, max_drawdown_stop_pct=stop)
    return RiskManager(settings, on_event=events.append), events


def test_drawdown_from_peak() -> None:
    rm, _ = manager()
    rm.on_equity(1, 10_000)
    rm.on_equity(2, 12_000)
    rm.on_equity(3, 10_800)
    assert rm.drawdown_pct() == 10.0
    assert rm.state.equity_peak == 12_000


def test_drawdown_stop_halts_once() -> None:
    rm, events = manager(stop=20)
    rm.on_equity(1, 10_000)
    assert not rm.on_equity(2, 8_100)
    assert rm.on_equity(3, 7_900)  # −21% — остановка
    assert rm.state.halted and rm.state.halt_reason == "max_drawdown"
    assert not rm.on_equity(4, 7_000)  # повторно не срабатывает
    assert [e.type for e in events] == ["drawdown_stop"]


def test_stop_disabled_by_default() -> None:
    rm = RiskManager(RiskSettings())
    rm.on_equity(1, 10_000)
    assert not rm.on_equity(2, 1_000)
    assert not rm.state.halted


def test_resume_resets_peak() -> None:
    rm, _ = manager(stop=20)
    rm.on_equity(1, 10_000)
    rm.on_equity(2, 7_000)
    rm.resume()
    assert not rm.state.halted
    assert rm.drawdown_pct() == 0
    assert not rm.on_equity(3, 6_500)  # −7% от нового пика


def test_kill_switch_event() -> None:
    rm, events = manager()
    rm.halt(5, "kill_switch")
    assert rm.state.halted and events[0].type == "kill_switch"


def test_state_roundtrip_ignores_unknown_fields() -> None:
    rm, _ = manager()
    rm.on_equity(1, 10_000)
    rm.on_equity(2, 9_000)
    data = rm.to_dict()
    data["open"] = {"BTCUSDT": {}}  # поле старой версии состояния
    state = RiskManager.state_from_dict(data)
    assert state.equity_peak == 10_000 and state.last_equity == 9_000


def test_circuit_breaker() -> None:
    cb = CircuitBreaker()
    tripped = [cb.record(1000 * i) for i in range(10)]
    assert any(tripped)
    cb.reset()
    assert not cb.tripped
