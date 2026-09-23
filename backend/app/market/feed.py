"""Поток закрытых свечей: история из REST + live из WebSocket, без пропусков и дублей."""

import time
from collections.abc import Awaitable, Callable

import structlog

from app.brokers.base import BrokerAdapter, CandleClosed, StreamReconnected
from app.domain import Candle, Timeframe
from app.market.store import CandleStore

log = structlog.get_logger()

CandleHandler = Callable[[CandleClosed], Awaitable[None]]


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


class CandleFeed:
    def __init__(
        self,
        broker: BrokerAdapter,
        store: CandleStore,
        subscriptions: list[tuple[str, Timeframe]],
        handler: CandleHandler,
        *,
        history_bars: int = 2500,
        clock: Callable[[], int] = wall_clock_ms,
    ) -> None:
        self._broker = broker
        self._store = store
        self._subs = list(dict.fromkeys(subscriptions))
        self._handler = handler
        self._history_bars = history_bars
        self._clock = clock
        self._last: dict[tuple[str, Timeframe], int] = {}

    async def run(self) -> None:
        await self.sync_all()
        async for event in self._broker.stream_candles(self._subs):
            if isinstance(event, StreamReconnected):
                log.info("feed.resync_after_reconnect")
                await self.sync_all()
            else:
                await self.on_ws_candle(event)

    async def sync_all(self) -> None:
        for symbol, tf in self._subs:
            await self.sync(symbol, tf)

    async def sync(self, symbol: str, tf: Timeframe) -> None:
        """Докачивает закрытые свечи через REST от последней сохранённой до текущего момента."""
        key = (symbol, tf)
        now = self._clock()
        last = self._last.get(key)
        if last is None:
            last = await self._store.last_ts(symbol, tf)
        start = last + tf.ms if last is not None else (now // tf.ms - self._history_bars) * tf.ms
        if start + tf.ms > now:
            if last is not None:
                self._last[key] = last
            return
        candles = await self._broker.get_candles(symbol, tf, start, now)
        closed = [c for c in candles if c.ts + tf.ms <= now and c.ts >= start]
        if closed:
            await self._store.save_candles(symbol, tf, closed)
            self._last[key] = closed[-1].ts
            log.info("feed.synced", symbol=symbol, tf=tf.value, bars=len(closed))
            await self._emit_if_fresh(symbol, tf, closed[-1], now)
        elif last is not None:
            self._last[key] = last

    async def on_ws_candle(self, event: CandleClosed) -> None:
        key = (event.symbol, event.timeframe)
        if key not in self._subs:
            return
        last = self._last.get(key)
        ts = event.candle.ts
        if last is not None and ts <= last:
            return  # дубль или уже докачана через REST
        if last is None or ts > last + event.timeframe.ms:
            await self.sync(event.symbol, event.timeframe)
            last = self._last.get(key)
            if last is not None and ts <= last:
                return
        await self._store.save_candles(event.symbol, event.timeframe, [event.candle])
        self._last[key] = ts
        await self._safe_handle(event)

    async def _emit_if_fresh(self, symbol: str, tf: Timeframe, candle: Candle, now: int) -> None:
        # Старые свечи из истории не отдаём стратегии: решение по устаревшим данным опасно.
        if now - (candle.ts + tf.ms) < tf.ms:
            await self._safe_handle(CandleClosed(symbol, tf, candle))

    async def _safe_handle(self, event: CandleClosed) -> None:
        try:
            await self._handler(event)
        except Exception:
            log.exception("feed.handler_failed", symbol=event.symbol, tf=event.timeframe.value)
