import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import RunMode, Settings
from app.core.events import Event, EventBus
from app.notify.telegram import TelegramNotifier, format_event
from app.risk.circuit import CircuitBreaker


def test_format_events() -> None:
    opened = format_event(
        Event(
            "trade_opened",
            {
                "symbol": "BTCUSDT",
                "direction": "long",
                "entry": 60000,
                "stop": 59000,
                "tp1": 61500,
                "tp2": 63000,
                "confidence": 77.4,
                "strategy": "trend",
            },
        )
    )
    assert opened is not None and "LONG BTCUSDT" in opened and "77%" in opened
    closed = format_event(
        Event("trade_closed", {"symbol": "ETHUSDT", "pnl": -12.5, "r_multiple": -1, "reason": "sl"})
    )
    assert closed is not None and "-12.50" in closed and "-1.00R" in closed
    assert format_event(Event("equity", {"equity": 1})) is None


class FakeEngine:
    def __init__(self) -> None:
        self.paused = False
        self.killed = False
        self.tracked: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "halted": self.killed,
            "halt_reason": None,
            "open_positions": 0,
            "risk_pct": 1.0,
            "drawdown_pct": 0.0,
        }

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    async def kill_switch(self) -> None:
        self.killed = True


def make_notifier(handler: Any = None) -> tuple[TelegramNotifier, FakeEngine]:
    engine = FakeEngine()
    ctx = SimpleNamespace(
        runtime=SimpleNamespace(engine=engine),
        settings=Settings(_env_file=None, mode=RunMode.PAPER),
    )
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={"ok": True})))
    n = TelegramNotifier("TOKEN", "42", EventBus(), ctx, transport=transport)  # type: ignore[arg-type]
    return n, engine


async def test_commands() -> None:
    n, engine = make_notifier()
    assert "Режим: paper" in (await n.handle_command("/status") or "")
    assert await n.handle_command("/pause") and engine.paused
    assert await n.handle_command("/resume") and not engine.paused
    assert "CONFIRM" in (await n.handle_command("/kill") or "")
    assert not engine.killed
    await n.handle_command("/kill CONFIRM")
    assert engine.killed
    assert await n.handle_command("просто текст") is None


async def test_commands_from_foreign_chat_ignored() -> None:
    sent: list[dict[str, Any]] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/getUpdates"):
            polls += 1
            if polls > 1:
                raise httpx.ConnectError("stop")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/kill CONFIRM"}},
                        {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/pause"}},
                    ],
                },
            )
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    n, engine = make_notifier(handler)
    import asyncio

    task = asyncio.create_task(n._poll_commands())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if sent:
            break
    task.cancel()
    assert not engine.killed  # команда из чужого чата проигнорирована
    assert engine.paused
    assert all(m["chat_id"] == "42" for m in sent)
    await n._http.aclose()


def test_from_settings_requires_token_and_chat() -> None:
    s = Settings(_env_file=None)
    assert TelegramNotifier.from_settings(s, EventBus(), None) is None  # type: ignore[arg-type]


def test_circuit_breaker() -> None:
    cb = CircuitBreaker(max_errors=3, window_ms=1000)
    assert not cb.record(0)
    assert not cb.record(600)
    assert not cb.record(1500)  # ошибка в t=0 вышла из окна: в окне 600 и 1500
    assert cb.record(1550)  # три ошибки за секунду
    assert cb.tripped
    assert not cb.record(1560)  # срабатывает один раз
    cb.reset()
    assert not cb.tripped


@pytest.mark.parametrize("secret", ["short"])
def test_short_jwt_secret_rejected(secret: str) -> None:
    from pydantic import SecretStr, ValidationError

    with pytest.raises(ValidationError, match="32"):
        Settings(_env_file=None, jwt_secret=SecretStr(secret))
