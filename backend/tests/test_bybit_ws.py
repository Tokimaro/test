"""WebSocket-клиент против локального сервера, имитирующего Bybit."""

import asyncio
import json
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from app.brokers.base import CandleClosed, StreamReconnected
from app.brokers.bybit.adapter import BybitAdapter
from app.brokers.bybit.client import BybitHttpClient
from app.brokers.bybit.ws import BybitStream
from app.domain import Timeframe


def kline_msg(ts: int, confirm: bool) -> str:
    return json.dumps(
        {
            "topic": "kline.60.BTCUSDT",
            "type": "snapshot",
            "data": [
                {
                    "start": ts,
                    "end": ts + 3_599_999,
                    "interval": "60",
                    "open": "1",
                    "high": "2",
                    "low": "0.5",
                    "close": "1.5",
                    "volume": "10",
                    "turnover": "15",
                    "confirm": confirm,
                    "timestamp": ts + 1000,
                }
            ],
        }
    )


class FakeBybit:
    """Каждое подключение: принимает subscribe, отдаёт свечи, затем рвёт связь (1-е подключение)."""

    def __init__(self) -> None:
        self.subscriptions: list[list[str]] = []
        self.pings = 0
        self.connections = 0

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        n = self.connections
        async for raw in ws:
            msg: dict[str, Any] = json.loads(raw)
            if msg["op"] == "subscribe":
                self.subscriptions.append(msg["args"])
                await ws.send(json.dumps({"success": True, "op": "subscribe"}))
                base = 1_700_000_000_000 + n * 3_600_000
                await ws.send(kline_msg(base, confirm=False))
                await ws.send(kline_msg(base, confirm=True))
                if n == 1:
                    await ws.close()
                    return
            elif msg["op"] == "ping":
                self.pings += 1
                await ws.send(json.dumps({"success": True, "ret_msg": "pong", "op": "ping"}))


async def test_stream_filters_confirmed_and_reconnects() -> None:
    fake = FakeBybit()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = BybitHttpClient(base_url="http://unused")
        adapter = BybitAdapter(
            client, ws_url=f"ws://127.0.0.1:{port}", ws_options={"reconnect_base_s": 0.01}
        )
        events: list[object] = []
        gen = adapter.stream_candles([("BTCUSDT", Timeframe.H1)])
        async with asyncio.timeout(5):
            async for ev in gen:
                events.append(ev)
                if len(events) == 3:
                    break
        await gen.aclose()
        await client.aclose()

    assert isinstance(events[0], CandleClosed)
    assert isinstance(events[1], StreamReconnected)
    assert isinstance(events[2], CandleClosed)
    assert events[2].candle.ts - events[0].candle.ts == 3_600_000
    assert fake.subscriptions[0] == ["kline.60.BTCUSDT"]
    assert fake.connections == 2


async def test_subscription_chunks_and_ping() -> None:
    fake = FakeBybit()
    fake.connections = 1  # сервер не будет рвать связь
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        topics = [f"kline.60.S{i}" for i in range(23)]
        stream = BybitStream(f"ws://127.0.0.1:{port}", topics, ping_interval_s=0.05)
        gen = stream.messages()
        async with asyncio.timeout(5):
            await anext(gen)  # первое сообщение с topic
            await asyncio.sleep(0.2)
        await gen.aclose()
    assert [len(c) for c in fake.subscriptions] == [10, 10, 3]
    assert fake.pings >= 2


async def test_watchdog_reconnects_silent_connection() -> None:
    connections = 0

    async def silent(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        async for raw in ws:
            if json.loads(raw)["op"] == "subscribe" and connections == 2:
                await ws.send(kline_msg(1_700_000_000_000, confirm=True))
            # на ping не отвечаем — соединение «мёртвое»

    async with serve(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = BybitStream(
            f"ws://127.0.0.1:{port}",
            ["kline.60.BTCUSDT"],
            ping_interval_s=0.05,
            reconnect_base_s=0.01,
        )
        gen = stream.messages()
        async with asyncio.timeout(5):
            first = await anext(gen)
            second = await anext(gen)
        await gen.aclose()
    assert isinstance(first, StreamReconnected)
    assert isinstance(second, dict)
    assert connections == 2
