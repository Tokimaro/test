"""Интеграция торгового движка: PostgreSQL + paper-брокер, полный цикл сделки."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.regime import Regime
from app.brokers.base import CandleClosed
from app.brokers.paper import PaperBroker
from app.config import BACKEND_DIR
from app.core.engine import TradingEngine
from app.core.events import Event, EventBus
from app.db.candles import upsert_instrument
from app.db.models import OrderRow, SignalRow, TradeRow
from app.db.repo import TradeRepo
from app.domain import Candle, Direction, Timeframe
from app.market.store import InMemoryCandleStore
from app.strategy.base import MarketContext
from app.strategy.ensemble import Signal, SignalEngine
from app.trading_config import TradingConfig
from tests.fakes import FakeMarketBroker
from tests.synthetic import frame_to_candles, make_ohlcv, resample

pytestmark = pytest.mark.db

H = Timeframe.H1.ms
M15 = Timeframe.M15.ms
T0 = 1_700_006_400_000
SYMBOL = "BTCUSDT"

RAW = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml").model_dump(mode="json")
RAW["markets"] = {
    "crypto": {**RAW["markets"]["crypto"], "symbols": [SYMBOL]},
}
CONFIG = TradingConfig.from_dict(RAW)


class StubSignals(SignalEngine):
    """Сигнал задаётся тестом; остальное (признаки, план, риск) — настоящее."""

    def __init__(self) -> None:
        super().__init__(CONFIG.strategy)
        self.next: Direction | None = Direction.LONG
        self.confidence = 90.0

    def evaluate_last(
        self, prepared: pd.DataFrame, symbol: str, ctx: MarketContext | None = None
    ) -> Signal:
        return Signal(
            int(prepared.index[-1]), symbol, self.next, self.confidence, Regime.TREND_UP, "trend"
        )


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@dataclass
class Env:
    engine: TradingEngine
    paper: PaperBroker
    sm: async_sessionmaker[AsyncSession]
    events: list[Event]
    clock: Clock
    last_close: float
    store: InMemoryCandleStore
    repo: TradeRepo
    iid: int


async def make_engine(env: Env | None, sm: async_sessionmaker[AsyncSession]) -> Env:
    if env is None:
        n = 2600
        h1 = make_ohlcv(n, drift=0.0008, vol=0.006, seed=4, start_ts=T0)
        store = InMemoryCandleStore()
        await store.save_candles(SYMBOL, Timeframe.H1, frame_to_candles(h1))
        await store.save_candles(
            SYMBOL, Timeframe.H4, frame_to_candles(resample(h1, Timeframe.H1, Timeframe.H4))
        )
        m15 = make_ohlcv(
            400,
            vol=0.002,
            seed=5,
            tf=Timeframe.M15,
            start_ts=T0 + n * H - 400 * M15,
            start_price=float(h1["close"].iloc[-1]),
        )
        await store.save_candles(SYMBOL, Timeframe.M15, frame_to_candles(m15))
        clock = Clock(T0 + n * H)
        paper = PaperBroker(
            FakeMarketBroker(), initial_equity=Decimal(10_000), slippage_pct=Decimal(0), clock=clock
        )
        last_close = float(h1["close"].iloc[-1])
        await paper.on_candle(
            SYMBOL, Candle(clock.now - M15, last_close, last_close, last_close, last_close, 1)
        )
        inst = await paper.get_instrument(SYMBOL)
        async with sm() as s, s.begin():
            iid = await upsert_instrument(s, "paper", inst)
        repo = TradeRepo(sm, "paper")
    else:
        store, clock, paper, last_close, repo, iid = (
            env.store,
            env.clock,
            env.paper,
            env.last_close,
            env.repo,
            env.iid,
        )
    bus = EventBus()
    q = bus.subscribe()
    events: list[Event] = []
    engine = TradingEngine(
        config=CONFIG,
        brokers={"crypto": paper},
        store=store,
        repo=repo,
        bus=bus,
        instrument_ids={SYMBOL: iid},
        clock=clock,
    )
    engine.signal_engine = StubSignals()
    await engine.start()

    async def drain() -> None:
        while not q.empty():
            events.append(q.get_nowait())

    engine.bus.publish = _tap(engine.bus.publish, events)  # type: ignore[method-assign]
    await drain()
    return Env(engine, paper, sm, events, clock, last_close, store, repo, iid)


def _tap(publish: Any, sink: list[Event]) -> Any:
    def wrapper(type_: str, **data: Any) -> None:
        sink.append(Event(type_, data))
        publish(type_, **data)

    return wrapper


@pytest.fixture
async def env(db_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    yield await make_engine(None, db_sessionmaker)


async def working_close(e: Env) -> None:
    ts = e.clock.now - H
    c = e.last_close
    await e.engine.on_candle(CandleClosed(SYMBOL, Timeframe.H1, Candle(ts, c, c, c, c, 1)))


async def price(e: Env, o: float, h: float, lo: float, c: float) -> None:
    e.clock.now += M15
    candle = Candle(e.clock.now - M15, o, h, lo, c, 1)
    await e.engine.on_candle(CandleClosed(SYMBOL, Timeframe.M15, candle))


async def trades(e: Env) -> list[TradeRow]:
    async with e.sm() as s:
        return list((await s.scalars(select(TradeRow).order_by(TradeRow.id))).all())


async def test_open_then_stop_loss(env: Env) -> None:
    await working_close(env)
    (t,) = await trades(env)
    assert t.status == "open"
    assert t.signal_id is not None
    pos = env.paper.positions[SYMBOL]
    assert pos.stop == t.stop_loss and pos.take_profit == t.tp2
    assert pos.stop < t.entry_price < t.tp1 < t.tp2  # type: ignore[operator]
    assert float(t.risk_amount) <= 100.0 + 1e-6  # 1% от 10k
    async with env.sm() as s:
        orders = list((await s.scalars(select(OrderRow).order_by(OrderRow.id))).all())
    assert [o.purpose for o in orders] == ["entry", "tp1"]
    assert all(o.status in ("Filled", "New") for o in orders)
    assert any(e.type == "trade_opened" for e in env.events)

    stop = float(t.stop_loss)
    await price(env, stop + 1, stop + 2, stop - 5, stop - 3)
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "closed"
    assert t.close_reason == "sl"
    # объём считался с запасом на проскальзывание, в paper его нет — потеря чуть меньше 1R
    assert t.r_multiple is not None
    assert -1.0 <= t.r_multiple <= -0.85
    assert SYMBOL not in env.engine.tracked
    assert env.engine.risk.state.open == {}
    assert any(e.type == "trade_closed" for e in env.events)
    assert await env.repo.get_state("risk") is not None


async def test_tp1_moves_stop_to_breakeven_then_tp2(env: Env) -> None:
    await working_close(env)
    (t,) = await trades(env)
    tp1, tp2 = float(t.tp1), float(t.tp2)  # type: ignore[arg-type]
    await price(env, tp1 - 1, tp1 + 1, tp1 - 2, tp1 + 0.5)
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.tp1_done
    assert float(t.stop_loss) > float(t.entry_price)  # type: ignore[arg-type]
    assert env.paper.positions[SYMBOL].stop == t.stop_loss
    await price(env, tp1 + 1, tp2 + 1, tp1, tp2 + 0.5)
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "closed" and t.close_reason == "tp2"
    # 0.5×1.5R + 0.5×3R = 2.25R брутто; минус комиссии и запас на проскальзывание в R
    assert t.r_multiple is not None
    assert 1.8 <= t.r_multiple <= 2.25


async def test_no_duplicate_entry_and_signals_logged(env: Env) -> None:
    await working_close(env)
    env.clock.now += H
    await working_close(env)
    assert len(await trades(env)) == 1
    async with env.sm() as s:
        sigs = list((await s.scalars(select(SignalRow).order_by(SignalRow.id))).all())
    assert [sg.acted for sg in sigs] == [True, False]
    assert sigs[1].reject_reason == "position_open"


async def test_below_threshold_is_not_traded(env: Env) -> None:
    assert isinstance(env.engine.signal_engine, StubSignals)
    env.engine.signal_engine.confidence = 50
    await working_close(env)
    assert await trades(env) == []


async def test_time_stop_closes_position(env: Env) -> None:
    await working_close(env)
    stub = env.engine.signal_engine
    assert isinstance(stub, StubSignals)
    stub.next = None
    for _ in range(CONFIG.strategy.stops.time_stop_bars):
        env.clock.now += H
        await working_close(env)
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "closed"
    assert t.close_reason == "time"


async def test_missing_stop_is_restored(env: Env) -> None:
    await working_close(env)
    env.paper.positions[SYMBOL].stop = None
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert env.paper.positions[SYMBOL].stop == t.stop_loss
    assert any(e.data.get("kind") == "missing_stop" for e in env.events)


async def test_kill_switch(env: Env) -> None:
    await working_close(env)
    await env.engine.kill_switch()
    (t,) = await trades(env)
    assert t.status == "closed" and t.close_reason == "kill"
    assert env.paper.positions == {}
    assert env.engine.risk.state.halted
    env.clock.now += H
    await working_close(env)
    assert len(await trades(env)) == 1  # после kill switch новых входов нет


async def test_restart_recovers_open_trade(
    env: Env, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await working_close(env)
    restarted = await make_engine(env, db_sessionmaker)
    assert SYMBOL in restarted.engine.tracked
    tr = restarted.engine.tracked[SYMBOL]
    (t,) = await trades(env)
    assert tr.trade_id == t.id
    assert tr.pos.stop == float(t.stop_loss)
    assert SYMBOL in restarted.engine.risk.state.open


async def test_unmanaged_position_alert(env: Env) -> None:
    from app.brokers.base import OrderRequest

    await env.paper.on_candle("ETHUSDT", Candle(env.clock.now, 10, 10, 10, 10, 1))
    await env.paper.place_order(
        OrderRequest(symbol="ETHUSDT", direction=Direction.LONG, qty=Decimal(1), link_id="manual")
    )
    await env.engine.reconcile()
    await env.engine.reconcile()
    alerts = [e for e in env.events if e.data.get("kind") == "unmanaged_position"]
    assert len(alerts) == 1


# ------------------------------------------------------------------ регрессии по код-ревью
async def test_tp1_failure_keeps_position_tracked(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.brokers.base import BrokerError, OrderRequest, OrderType

    original = env.paper.place_order

    async def no_limits(req: OrderRequest) -> Any:
        if req.order_type is OrderType.LIMIT:
            raise BrokerError("price out of range", code=110003)
        return await original(req)

    monkeypatch.setattr(env.paper, "place_order", no_limits)
    await working_close(env)
    (t,) = await trades(env)
    assert t.status == "open"
    tr = env.engine.tracked[SYMBOL]
    assert tr.confirmed and tr.pos.tp1 is None and tr.pos.trailing_active()
    assert SYMBOL in env.engine.risk.state.open
    assert any(e.data.get("kind") == "tp1_failed" for e in env.events)


async def test_unmanaged_exchange_position_blocks_entry(env: Env) -> None:
    from app.brokers.base import OrderRequest

    await env.paper.place_order(
        OrderRequest(symbol=SYMBOL, direction=Direction.SHORT, qty=Decimal("0.01"), link_id="m")
    )
    await working_close(env)
    assert await trades(env) == []
    async with env.sm() as s:
        sig = await s.scalar(select(SignalRow))
    assert sig is not None and sig.reject_reason == "exchange_position_exists"


async def test_uncertain_entry_is_adopted_when_position_appears(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.brokers.base import BrokerError, OrderRequest, OrderType

    original = env.paper.place_order

    async def lost_response(req: OrderRequest) -> Any:
        res = await original(req)
        if req.order_type is OrderType.MARKET and not req.reduce_only:
            raise BrokerError("network: read timeout")  # ордер исполнен, ответ потерян
        return res

    async def lookup_down(symbol: str, link_id: str) -> Any:
        raise BrokerError("network: connect timeout")

    monkeypatch.setattr(env.paper, "place_order", lost_response)
    monkeypatch.setattr(env.paper, "get_order", lookup_down)
    await working_close(env)
    (t,) = await trades(env)
    assert t.status == "pending"
    assert not env.engine.tracked[SYMBOL].confirmed
    # пока исход неизвестен — новых входов нет
    env.clock.now += H
    await working_close(env)
    assert len(await trades(env)) == 1
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "open"
    assert env.engine.tracked[SYMBOL].confirmed
    assert SYMBOL in env.engine.risk.state.open


async def test_uncertain_entry_cancelled_if_never_filled(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.brokers.base import BrokerError, OrderRequest
    from app.core.engine import CONFIRM_ATTEMPTS

    async def never(req: OrderRequest) -> Any:
        raise BrokerError("network: read timeout")

    async def not_found(symbol: str, link_id: str) -> Any:
        return None

    monkeypatch.setattr(env.paper, "place_order", never)
    monkeypatch.setattr(env.paper, "get_order", not_found)
    await working_close(env)
    for _ in range(CONFIRM_ATTEMPTS):
        await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "cancelled" and t.close_reason == "not_filled"
    assert SYMBOL not in env.engine.tracked


async def test_failed_close_is_retried(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.brokers.base import BrokerError

    await working_close(env)
    stub = env.engine.signal_engine
    assert isinstance(stub, StubSignals)
    stub.next = None
    executor = env.engine.executors["crypto"]
    original = executor.close_position
    calls = {"n": 0}

    async def flaky(trade_id: int, symbol: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BrokerError("service unavailable")
        await original(trade_id, symbol)

    monkeypatch.setattr(executor, "close_position", flaky)
    for _ in range(CONFIG.strategy.stops.time_stop_bars):
        env.clock.now += H
        await working_close(env)
    assert calls["n"] == 1
    assert env.engine.tracked[SYMBOL].pending_reason is None  # не «застряла»
    env.clock.now += H
    await working_close(env)  # тайм-стоп срабатывает повторно
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "closed" and t.close_reason == "time"


async def test_backfilled_candles_trigger_paper_stops(env: Env) -> None:
    await working_close(env)
    (t,) = await trades(env)
    stop = float(t.stop_loss)
    ts = env.clock.now
    missed = [
        Candle(ts, stop + 2, stop + 3, stop + 1, stop + 2, 1),
        Candle(ts + M15, stop + 2, stop + 3, stop - 1, stop + 2, 1),  # стоп внутри пропуска
        Candle(ts + 2 * M15, stop + 2, stop + 4, stop + 1, stop + 3, 1),
    ]
    env.clock.now += 3 * M15
    await env.engine.on_backfill(SYMBOL, Timeframe.M15, missed)
    await env.engine.reconcile()
    (t,) = await trades(env)
    assert t.status == "closed" and t.close_reason == "sl"


async def test_restart_with_pending_trade_adopts_filled_position(
    env: Env, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await working_close(env)
    (t,) = await trades(env)
    # имитируем падение процесса между исполнением входа и записью статуса open
    await env.repo.update_trade(t.id, status="pending", entry_price=None)
    restarted = await make_engine(env, db_sessionmaker)
    (t,) = await trades(env)
    assert t.status == "open"
    assert restarted.engine.tracked[SYMBOL].confirmed


async def test_circuit_breaker_pauses_after_api_errors(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.brokers.base import BrokerError

    async def down() -> Any:
        raise BrokerError("503")

    monkeypatch.setattr(env.paper, "get_positions", down)
    for _ in range(env.engine.breaker.max_errors):
        await env.engine.reconcile()
    assert env.engine.paused and env.engine.breaker.tripped
    assert any(e.data.get("kind") == "circuit_breaker" for e in env.events)
    env.engine.resume()
    assert not env.engine.paused and not env.engine.breaker.tripped
