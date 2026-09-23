"""Тестовые двойники."""

from collections.abc import AsyncGenerator
from decimal import Decimal

from app.brokers.base import (
    BrokerAdapter,
    ClosedPnl,
    OrderRequest,
    OrderResult,
    StreamEvent,
)
from app.domain import Balance, Candle, Instrument, MarketType, Position, Timeframe


def make_candle(ts: int, close: float = 100.0) -> Candle:
    return Candle(ts=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


class FakeMarketBroker(BrokerAdapter):
    """Брокер с заранее заданной историей свечей и сценарием событий потока."""

    name = "fake"

    def __init__(self, history: dict[tuple[str, Timeframe], list[Candle]] | None = None) -> None:
        self.history = history or {}
        self.events: list[StreamEvent] = []
        self.candle_requests: list[tuple[str, Timeframe, int, int]] = []

    def market_type(self) -> MarketType:
        return MarketType.CRYPTO

    async def get_instrument(self, symbol: str) -> Instrument:
        return Instrument(
            symbol=symbol,
            market_type=MarketType.CRYPTO,
            category="linear",
            tick_size=Decimal("0.1"),
            qty_step=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal(1000),
            max_leverage=Decimal(50),
        )

    async def get_candles(
        self, symbol: str, timeframe: Timeframe, start_ms: int, end_ms: int
    ) -> list[Candle]:
        self.candle_requests.append((symbol, timeframe, start_ms, end_ms))
        return [c for c in self.history.get((symbol, timeframe), []) if start_ms <= c.ts <= end_ms]

    async def stream_candles(
        self, subscriptions: list[tuple[str, Timeframe]]
    ) -> AsyncGenerator[StreamEvent]:
        for ev in self.events:
            yield ev

    async def server_time_ms(self) -> int:
        return 0

    async def get_balance(self) -> Balance:
        raise NotImplementedError

    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        raise NotImplementedError

    async def place_order(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError

    async def get_order(self, symbol: str, link_id: str) -> OrderResult | None:
        raise NotImplementedError

    async def get_closed_pnl(self, symbol: str, since_ms: int) -> list[ClosedPnl]:
        raise NotImplementedError

    async def amend_stops(
        self, symbol: str, stop_loss: Decimal | None = None, take_profit: Decimal | None = None
    ) -> None:
        raise NotImplementedError

    async def close_position(self, symbol: str, qty: Decimal | None = None) -> OrderResult | None:
        raise NotImplementedError

    async def cancel_all(self, symbol: str | None = None) -> None:
        raise NotImplementedError
