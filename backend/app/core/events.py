"""Внутренняя шина событий: движок публикует, панель (WebSocket) и Telegram подписываются."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class Event:
    type: str  # signal / entry_placed / entry_expired / trade_opened / trade_updated /
    # trade_closed / equity / alert / bot_status
    data: dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=lambda: int(time.time() * 1000))


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._subs: set[asyncio.Queue[Event]] = set()
        self._size = queue_size

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._size)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.discard(q)

    def publish(self, type_: str, **data: Any) -> None:
        event = Event(type_, data)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("bus.subscriber_lagging", event=type_)
