from app.brokers.base import CandleClosed, StreamReconnected
from app.domain import Timeframe
from app.market.feed import CandleFeed
from app.market.store import InMemoryCandleStore
from tests.fakes import FakeMarketBroker, make_candle

H = Timeframe.H1.ms
T0 = 1_700_000_000_000 // H * H
KEY = ("BTCUSDT", Timeframe.H1)


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def make_feed(
    broker: FakeMarketBroker, store: InMemoryCandleStore, clock: Clock, history_bars: int = 10
) -> tuple[CandleFeed, list[CandleClosed]]:
    got: list[CandleClosed] = []

    async def handler(ev: CandleClosed) -> None:
        got.append(ev)

    feed = CandleFeed(broker, store, [KEY], handler, history_bars=history_bars, clock=clock)
    return feed, got


async def test_initial_backfill_saves_closed_and_emits_only_latest() -> None:
    # 20 свечей истории, последняя (T0+20H) ещё формируется
    broker = FakeMarketBroker({KEY: [make_candle(T0 + i * H) for i in range(21)]})
    store = InMemoryCandleStore()
    clock = Clock(T0 + 20 * H + H // 2)
    feed, got = make_feed(broker, store, clock)
    await feed.sync_all()
    stored = await store.get_candles(*KEY)
    assert [c.ts for c in stored] == [T0 + i * H for i in range(10, 20)]  # history_bars=10
    assert [e.candle.ts for e in got] == [T0 + 19 * H]


async def test_stale_history_is_not_emitted() -> None:
    broker = FakeMarketBroker({KEY: [make_candle(T0 + i * H) for i in range(5)]})
    store = InMemoryCandleStore()
    clock = Clock(T0 + 50 * H)  # биржа отдала историю, но последняя свеча давно в прошлом
    feed, got = make_feed(broker, store, clock, history_bars=100)
    await feed.sync_all()
    assert len(await store.get_candles(*KEY)) == 5
    assert got == []


async def test_resume_from_store_requests_only_missing() -> None:
    store = InMemoryCandleStore()
    await store.save_candles(*KEY, [make_candle(T0 + i * H) for i in range(5)])
    broker = FakeMarketBroker({KEY: [make_candle(T0 + i * H) for i in range(8)]})
    clock = Clock(T0 + 8 * H)
    feed, got = make_feed(broker, store, clock)
    await feed.sync_all()
    assert broker.candle_requests[0][2] == T0 + 5 * H
    assert (await store.last_ts(*KEY)) == T0 + 7 * H
    assert [e.candle.ts for e in got] == [T0 + 7 * H]


async def test_ws_flow_dedup_gap_and_reconnect() -> None:
    history = [make_candle(T0 + i * H) for i in range(10)]
    broker = FakeMarketBroker({KEY: history})
    store = InMemoryCandleStore()
    clock = Clock(T0 + 3 * H)
    feed, got = make_feed(broker, store, clock)
    broker.events = [
        CandleClosed("BTCUSDT", Timeframe.H1, history[2]),  # дубль уже скачанной
        CandleClosed("BTCUSDT", Timeframe.H1, history[3]),  # следующая — ок
        CandleClosed("BTCUSDT", Timeframe.H1, history[6]),  # пропуск 4,5 → докачка
        StreamReconnected(),
        CandleClosed("ETHUSDT", Timeframe.H1, history[7]),  # не подписаны — игнор
    ]

    # время идёт вперёд вместе с событиями
    orig = feed.on_ws_candle

    async def on_ws(ev: CandleClosed) -> None:
        clock.now = ev.candle.ts + H + 1
        await orig(ev)

    feed.on_ws_candle = on_ws  # type: ignore[method-assign,assignment]
    await feed.run()

    stored = [c.ts for c in await store.get_candles(*KEY)]
    assert stored == [T0 + i * H for i in range(7)]  # без дыр
    emitted = [e.candle.ts for e in got]
    assert emitted == [T0 + 2 * H, T0 + 3 * H, T0 + 6 * H]
    assert len(emitted) == len(set(emitted))


async def test_handler_error_does_not_break_feed() -> None:
    broker = FakeMarketBroker({KEY: [make_candle(T0)]})
    store = InMemoryCandleStore()
    calls = 0

    async def boom(ev: CandleClosed) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("strategy crashed")

    feed = CandleFeed(broker, store, [KEY], boom, clock=Clock(T0 + H + 1))
    broker.events = [CandleClosed("BTCUSDT", Timeframe.H1, make_candle(T0 + H))]
    await feed.run()
    assert calls == 2
    assert (await store.last_ts(*KEY)) == T0 + H
