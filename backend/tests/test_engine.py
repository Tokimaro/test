"""Интеграция торгового движка: PostgreSQL + paper-брокер (спот), полный цикл портфеля."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.base import CandleClosed
from app.brokers.paper import PaperBroker
from app.core.engine import DAY_MS, EVAL_GRACE_MS, TradingEngine
from app.core.events import Event, EventBus
from app.db.candles import upsert_instrument
from app.db.models import OrderRow, SignalRow, TradeRow
from app.db.repo import TradeRepo
from app.domain import Candle, Direction, Timeframe
from app.market.store import InMemoryCandleStore
from app.trading_config import TradingConfig
from tests.fakes import FakeMarketBroker

pytestmark = pytest.mark.db

SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
HISTORY = 200
# история заканчивается свечой среды: первый расчёт сразу ребалансирует (первый запуск),
# следующий плановый — по закрытию воскресной свечи
DAY0 = 1_700_006_400_000 // DAY_MS * DAY_MS
while datetime.fromtimestamp((DAY0 + HISTORY * DAY_MS) / 1000, tz=UTC).weekday() != 3:
    DAY0 += DAY_MS
LAST = DAY0 + (HISTORY - 1) * DAY_MS  # открытие последней загруженной свечи


def config(**risk: Any) -> TradingConfig:
    return TradingConfig.from_dict(
        {
            "markets": {"crypto": {"market_type": "crypto", "category": "spot", "symbols": SYMS}},
            "risk": {"profile": "custom", **risk} if risk else {},
        }
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
    store: InMemoryCandleStore
    repo: TradeRepo
    ids: dict[str, int]
    last: dict[str, float]
    day: int  # открытие последней закрытой свечи


def series(drift: float, seed: int, n: int = HISTORY) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))


async def build(
    sm: async_sessionmaker[AsyncSession], cfg: TradingConfig | None = None, env: Env | None = None
) -> Env:
    if env is None:
        store = InMemoryCandleStore()
        last = {}
        for s, drift, seed in (
            ("BTCUSDT", 0.004, 1),
            ("ETHUSDT", 0.004, 2),
            ("SOLUSDT", -0.006, 3),
        ):
            closes = series(drift, seed)
            candles = [
                Candle(DAY0 + i * DAY_MS, c, c * 1.01, c * 0.99, c, 1.0)
                for i, c in enumerate(closes)
            ]
            await store.save_candles(s, Timeframe.D1, candles)
            last[s] = float(closes[-1])
        clock = Clock(LAST + DAY_MS + 60_000)
        paper = PaperBroker(
            FakeMarketBroker(spot=True),
            initial_equity=Decimal(10_000),
            slippage_pct=Decimal(0),
            clock=clock,
        )
        repo = TradeRepo(sm, "paper")
        ids = {}
        for s in SYMS:
            async with sm() as ses, ses.begin():
                ids[s] = await upsert_instrument(ses, "paper", await paper.get_instrument(s))
        day = LAST
    else:
        store, clock, paper, repo, ids, last, day = (
            env.store,
            env.clock,
            env.paper,
            env.repo,
            env.ids,
            env.last,
            env.day,
        )
    events: list[Event] = []
    bus = EventBus()
    engine = TradingEngine(
        config=cfg or config(),
        brokers={"crypto": paper},
        store=store,
        repo=repo,
        bus=bus,
        instrument_ids=ids,
        clock=clock,
    )
    engine.bus.publish = _tap(engine.bus.publish, events)  # type: ignore[method-assign]
    await engine.start()
    return Env(engine, paper, sm, events, clock, store, repo, ids, last, day)


def _tap(publish: Any, sink: list[Event]) -> Any:
    def wrapper(type_: str, **data: Any) -> None:
        sink.append(Event(type_, data))
        publish(type_, **data)

    return wrapper


@pytest.fixture
async def env(db_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    yield await build(db_sessionmaker)


async def close_day(e: Env, moves: dict[str, float] | None = None, day: int | None = None) -> None:
    """Приходят закрытые дневные свечи всех монет (moves — изменение цены за день)."""
    day = e.day if day is None else day
    for s in SYMS:
        if day > e.day:
            e.last[s] *= 1 + (moves or {}).get(s, 0.004)
            c = e.last[s]
            await e.store.save_candles(s, Timeframe.D1, [Candle(day, c, c, c, c, 1.0)])
        c = e.last[s]
        await e.engine.on_candle(CandleClosed(s, Timeframe.D1, Candle(day, c, c, c, c, 1.0)))
    e.day = day


async def next_day(e: Env, moves: dict[str, float] | None = None) -> None:
    e.clock.now += DAY_MS
    await close_day(e, moves, e.day + DAY_MS)


async def trades(e: Env) -> list[TradeRow]:
    async with e.sm() as s:
        return list((await s.scalars(select(TradeRow).order_by(TradeRow.id))).all())


async def orders(e: Env) -> list[OrderRow]:
    async with e.sm() as s:
        return list((await s.scalars(select(OrderRow).order_by(OrderRow.id))).all())


async def test_first_evaluation_buys_uptrend_coins(env: Env) -> None:
    await close_day(env)
    held = set(env.engine.holdings)
    assert held == {"BTCUSDT", "ETHUSDT"}  # SOL падает — в кэше
    assert env.engine.targets["SOLUSDT"] == 0
    assert set(env.paper.positions) == held
    ts = await trades(env)
    assert {t.status for t in ts} == {"open"} and len(ts) == 2
    for t in ts:
        assert t.entry_price and t.qty > 0 and t.stop_loss is None
        assert t.signal_id is not None  # владение связано с сигналом дня
        assert float(t.fees) > 0  # комиссия спота 0.1%
    assert all(o.status == "Filled" and o.side == "long" for o in await orders(env))
    async with env.sm() as s:
        sigs = list((await s.scalars(select(SignalRow))).all())
    assert len(sigs) == 3 and all(sg.acted for sg in sigs)
    # доли не превышают капитал; остаток — в USDT
    bal = await env.paper.get_balance()
    assert bal.available >= 0
    kinds = [e.type for e in env.events]
    assert kinds.count("trade_opened") == 2 and "rebalance" in kinds


async def test_no_trades_between_rebalance_days(env: Env) -> None:
    await close_day(env)
    n = len(await orders(env))
    for _ in range(3):  # чт, пт, сб — не день ребалансировки
        await next_day(env)
    assert len(await orders(env)) == n
    async with env.sm() as s:
        sigs = list((await s.scalars(select(SignalRow).where(SignalRow.acted.is_(False)))).all())
    assert len(sigs) == 9 and {sg.reject_reason for sg in sigs} == {"not_rebalance_day"}


async def test_trend_loss_sells_on_rebalance_day(env: Env) -> None:
    await close_day(env)
    for _ in range(3):
        await next_day(env)
    # воскресная свеча закрывается → понедельник: ETH рухнул за 30 дней
    for _ in range(25):
        env.clock.now += DAY_MS
        env.day += DAY_MS
        for s in SYMS:
            env.last[s] *= 0.93 if s == "ETHUSDT" else 1.004
            c = env.last[s]
            await env.store.save_candles(s, Timeframe.D1, [Candle(env.day, c, c, c, c, 1.0)])
            await env.engine.on_candle(
                CandleClosed(s, Timeframe.D1, Candle(env.day, c, c, c, c, 1))
            )
    assert "ETHUSDT" not in env.engine.holdings
    eth = [t for t in await trades(env) if t.instrument_id == env.ids["ETHUSDT"]]
    closed = [t for t in eth if t.status == "closed"]
    assert closed and closed[0].close_reason == "schedule"
    assert float(closed[0].realized_pnl) < 0 and closed[0].exit_price is not None
    assert closed[0].r_multiple is not None and closed[0].r_multiple < 0
    assert "BTCUSDT" in env.engine.holdings
    assert any(e.type == "trade_closed" for e in env.events)


async def test_restart_restores_holdings_without_rebuying(
    env: Env, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await close_day(env)
    n = len(await orders(env))
    again = await build(db_sessionmaker, env=env)
    assert set(again.engine.holdings) == set(env.engine.holdings)
    assert again.engine.last_rebalance == env.engine.last_rebalance
    await close_day(again)  # тот же день ещё раз — не пересчитывается
    assert len(await orders(again)) == n


async def test_restart_restores_prices_for_manual_rebalance(
    env: Env, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await close_day(env)
    await env.engine.close_manually("BTCUSDT")
    # после рестарта новых свечей нет: цены берутся из сохранённых дневных свечей
    again = await build(db_sessionmaker, env=env)
    assert set(again.engine.prices) == set(SYMS)
    results = await again.engine.rebalance_now()
    assert isinstance(results["crypto"], int) and results["crypto"] > 0
    assert "BTCUSDT" in again.engine.holdings


async def test_symbol_list_change_recomputes_targets(
    env: Env, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await close_day(env)
    n = len(await orders(env))
    closes = series(0.004, 4)
    await env.store.save_candles(
        "XRPUSDT",
        Timeframe.D1,
        [Candle(DAY0 + i * DAY_MS, c, c, c, c, 1.0) for i, c in enumerate(closes)],
    )
    async with db_sessionmaker() as ses, ses.begin():
        env.ids["XRPUSDT"] = await upsert_instrument(
            ses, "paper", await env.paper.get_instrument("XRPUSDT")
        )
    cfg = TradingConfig.from_dict(
        {
            "markets": {
                "crypto": {
                    "market_type": "crypto",
                    "category": "spot",
                    "symbols": [*SYMS, "XRPUSDT"],
                }
            },
            "risk": {},
        }
    )
    again = await build(db_sessionmaker, cfg, env=env)
    assert "XRPUSDT" in again.engine.prices
    # доли по старому списку не годятся: пока не пересчитаны — ручная ребалансировка ждёт
    assert await again.engine.rebalance_now() == {"crypto": "targets_pending"}
    again.clock.now += 20 * 60_000
    await again.engine.reconcile()  # догоняющий расчёт по новому списку, без сделок
    assert again.engine.targets["XRPUSDT"] > 0
    assert len(await orders(again)) == n


async def test_kill_switch_sells_everything(env: Env) -> None:
    await close_day(env)
    await env.engine.kill_switch()
    assert env.engine.holdings == {} and env.paper.positions == {}
    assert env.engine.paused and env.engine.risk.state.halted
    assert {t.close_reason for t in await trades(env)} == {"kill"}
    await next_day(env)  # в паузе новых покупок нет
    assert env.paper.positions == {}


async def test_manual_close_and_rebalance_now(env: Env) -> None:
    await close_day(env)
    await env.engine.close_manually("BTCUSDT")
    assert "BTCUSDT" not in env.engine.holdings
    btc = [t for t in await trades(env) if t.instrument_id == env.ids["BTCUSDT"]]
    assert btc[0].close_reason == "manual"
    await env.engine.rebalance_now()  # внеплановая — снова к целевым долям
    assert "BTCUSDT" in env.engine.holdings
    links = [o.link_id for o in await orders(env)]
    assert len(links) == len(set(links))  # id ордеров не повторяются


async def test_reconcile_syncs_wallet_and_detects_external_sale(env: Env) -> None:
    await close_day(env)
    env.paper.positions["BTCUSDT"].qty *= Decimal("0.999")  # комиссия списана в монете
    env.paper.positions.pop("ETHUSDT")  # продали вручную на бирже
    await env.engine.reconcile()
    btc = env.engine.holdings["BTCUSDT"]
    assert btc.qty == pytest.approx(float(env.paper.positions["BTCUSDT"].qty))
    assert "ETHUSDT" not in env.engine.holdings
    eth = [t for t in await trades(env) if t.instrument_id == env.ids["ETHUSDT"]]
    assert eth[0].close_reason == "external"
    assert any(e.type == "alert" and e.data.get("kind") == "holding_gone" for e in env.events)


async def test_drawdown_stop_liquidates(db_sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    e = await build(db_sessionmaker, config(max_drawdown_stop_pct=10))
    await close_day(e)
    await e.engine.reconcile()  # пик капитала
    for s in ("BTCUSDT", "ETHUSDT"):
        e.last[s] *= 0.6
        e.paper.mark_price(s, e.last[s], e.clock.now)
        e.engine.prices[s] = e.last[s]
    await e.engine.reconcile()
    assert e.engine.risk.state.halted and e.engine.paused
    assert e.engine.holdings == {}
    assert {t.close_reason for t in await trades(e)} == {"drawdown_stop"}


async def test_missed_evaluation_catches_up(env: Env) -> None:
    await close_day(env)
    n = len(await orders(env))
    # свеча следующего дня сохранена, но событие потока не пришло
    nxt = env.day + DAY_MS
    for s in SYMS:
        c = env.last[s]
        await env.store.save_candles(s, Timeframe.D1, [Candle(nxt, c, c, c, c, 1.0)])
    env.clock.now = nxt + DAY_MS + EVAL_GRACE_MS + 1
    await env.engine.reconcile()
    assert env.engine.last_eval["crypto"] == nxt
    assert len(await orders(env)) == n  # не понедельник — только расчёт


async def test_no_evaluation_on_stale_history(env: Env) -> None:
    """Свеча дня есть не у всех монет (история не докачана) — расчёта и сделок нет,
    пока данные не появятся."""
    await close_day(env)
    n = len(await orders(env))
    nxt = env.day + DAY_MS
    for s in ("BTCUSDT", "ETHUSDT"):  # у SOL свечи ещё нет
        c = env.last[s]
        await env.store.save_candles(s, Timeframe.D1, [Candle(nxt, c, c, c, c, 1.0)])
    env.clock.now = nxt + DAY_MS + EVAL_GRACE_MS + 1
    await env.engine.reconcile()
    assert env.engine.last_eval["crypto"] == env.day  # не пересчитано
    assert any(e.type == "alert" and e.data.get("kind") == "stale_data" for e in env.events)
    c = env.last["SOLUSDT"]
    await env.store.save_candles("SOLUSDT", Timeframe.D1, [Candle(nxt, c, c, c, c, 1.0)])
    await env.engine.reconcile()
    assert env.engine.last_eval["crypto"] == nxt
    assert len(await orders(env)) == n


async def test_foreign_coins_are_not_capital_and_not_sold(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    e = await build(db_sessionmaker)
    # до бота на счёте уже лежал SOL (монета из списка, но куплена не ботом)
    sol = await e.paper.get_instrument("SOLUSDT")
    e.paper.mark_price("SOLUSDT", e.last["SOLUSDT"], e.clock.now)
    e.paper._execute(
        sol,
        "SOLUSDT",
        Direction.LONG,
        Decimal(10),
        Decimal(str(e.last["SOLUSDT"])),
        Decimal(0),
        reduce_only=False,
    )
    await close_day(e)
    await e.engine.reconcile()
    assert "SOLUSDT" not in e.engine.holdings
    assert e.paper.positions["SOLUSDT"].qty == Decimal(10)  # чужие монеты не проданы
    assert any(x.type == "alert" and x.data.get("kind") == "unmanaged_holding" for x in e.events)


async def test_uncertain_buy_resolved_by_reconcile(env: Env) -> None:
    await close_day(env)
    btc = env.engine.holdings["BTCUSDT"]
    # как будто ответ на покупку ETH потерялся: владение без объёма, монеты на счёте есть
    eth = env.engine.holdings["ETHUSDT"]
    eth.qty, eth.invested, eth.avg_entry = 0.0, 0.0, 0.0
    await env.engine.reconcile()
    assert env.engine.holdings["ETHUSDT"].qty == pytest.approx(
        float(env.paper.positions["ETHUSDT"].qty)
    )
    assert env.engine.holdings["ETHUSDT"].avg_entry > 0
    # а здесь покупка не дошла до биржи — запись отменяется
    btc.qty, btc.invested = 0.0, 0.0
    env.paper.positions.pop("BTCUSDT")
    await env.engine.reconcile()
    assert "BTCUSDT" not in env.engine.holdings
    rows = {t.id: t for t in await trades(env)}
    assert rows[btc.trade_id].status == "cancelled"
