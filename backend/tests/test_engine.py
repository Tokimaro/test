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
