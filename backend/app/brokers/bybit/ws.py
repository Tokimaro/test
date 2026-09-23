"""WebSocket Bybit V5 с автоматическим переподключением, ping и сторожевым таймером."""

import asyncio
import json
import random
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from app.brokers.base import BrokerError, StreamReconnected

log = structlog.get_logger()

MAINNET_WS = "wss://stream.bybit.com/v5"
TESTNET_WS = "wss://stream-testnet.bybit.com/v5"

SUBSCRIBE_CHUNK = 10  # Bybit ограничивает число топиков в одном запросе

Connector = Callable[[str], Any]  # возвращает async context manager с ClientConnection


def public_url(category: str, testnet: bool) -> str:
    return f"{TESTNET_WS if testnet else MAINNET_WS}/public/{category}"


def _default_connect(url: str) -> Any:
    # ping делаем на уровне протокола Bybit ({"op": "ping"}), встроенный отключаем
    return websockets.connect(url, ping_interval=None, open_timeout=10, max_size=2**22)


class BybitStream:
    def __init__(
        self,
        url: str,
        topics: list[str],
        *,
        ping_interval_s: float = 20.0,
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        connect: Connector = _default_connect,
        on_open: Callable[[ClientConnection], Any] | None = None,
    ) -> None:
        if not topics:
            raise ValueError("нужна хотя бы одна подписка")
        self._url = url
        self._topics = topics
        self._ping_interval = ping_interval_s
        self._reconnect_base = reconnect_base_s
        self._reconnect_max = reconnect_max_s
        self._connect = connect
        self._on_open = on_open  # например, аутентификация приватного канала

    async def messages(self) -> AsyncGenerator[dict[str, Any] | StreamReconnected]:
        """Бесконечно отдаёт сообщения с полем topic. После каждого переподключения
        первым отдаётся StreamReconnected."""
        connected_before = False
        failures = 0
        while True:
            try:
                async with self._connect(self._url) as ws:
                    if self._on_open is not None:
                        await self._on_open(ws)
                    await self._subscribe(ws)
                    log.info("ws.connected", url=self._url, topics=len(self._topics))
                    if connected_before:
                        yield StreamReconnected()
                    connected_before = True
                    failures = 0
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for msg in self._receive(ws):
                            yield msg
                    finally:
                        ping_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await ping_task
            except (
                OSError,
                TimeoutError,
                json.JSONDecodeError,
                websockets.WebSocketException,
            ) as exc:
                failures += 1
                delay = min(self._reconnect_max, self._reconnect_base * 2 ** (failures - 1))
                delay *= random.uniform(0.8, 1.2)
                log.warning("ws.disconnected", url=self._url, error=repr(exc), retry_in_s=delay)
                await asyncio.sleep(delay)

    async def _subscribe(self, ws: ClientConnection) -> None:
        for i in range(0, len(self._topics), SUBSCRIBE_CHUNK):
            chunk = self._topics[i : i + SUBSCRIBE_CHUNK]
            await ws.send(json.dumps({"op": "subscribe", "args": chunk}))

    async def _receive(self, ws: ClientConnection) -> AsyncGenerator[dict[str, Any]]:
        # Сторожевой таймер: сервер отвечает на ping каждые ping_interval, поэтому тишина
        # дольше двух интервалов означает «мёртвое» соединение.
        watchdog = self._ping_interval * 2.5
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=watchdog)
            data = json.loads(raw)
            if "topic" in data:
                yield data
            elif data.get("op") in ("subscribe", "auth") and data.get("success") is False:
                raise BrokerError(f"ws {data.get('op')} отклонён: {data.get('ret_msg')}")

    async def _ping_loop(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send(json.dumps({"op": "ping"}))
