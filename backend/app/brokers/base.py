"""Единый интерфейс брокера. Стратегия и исполнение работают только через него,
поэтому один и тот же код торгует в бэктесте, paper и live, на крипте и на акциях."""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.domain import Balance, Candle, Direction, Instrument, MarketType, Position, Timeframe


class BrokerError(Exception):
    """Ошибка брокера. code — код биржи (для Bybit — retCode)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CandleClosed:
    symbol: str
    timeframe: Timeframe
    candle: Candle


@dataclass(frozen=True, slots=True)
class StreamReconnected:
    """Поток был переподключён: подписчику нужно докачать возможный пропуск через REST."""


StreamEvent = CandleClosed | StreamReconnected


class OrderType(StrEnum):
    MARKET = "Market"
    LIMIT = "Limit"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    direction: Direction
    qty: Decimal
    link_id: str  # клиентский id — делает повторную отправку идемпотентной
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: str
    link_id: str
    status: str = "New"
    avg_price: Decimal | None = None
    filled_qty: Decimal = Decimal(0)
    raw: dict[str, object] = field(default_factory=dict)


class BrokerAdapter(ABC):
    name: str

    @abstractmethod
    def market_type(self) -> MarketType: ...

    # --- рыночные данные ---
    @abstractmethod
    async def get_instrument(self, symbol: str) -> Instrument: ...

    @abstractmethod
    async def get_candles(
        self, symbol: str, timeframe: Timeframe, start_ms: int, end_ms: int
    ) -> list[Candle]:
        """Свечи с ts в [start_ms, end_ms], по возрастанию времени."""

    @abstractmethod
    def stream_candles(
        self, subscriptions: list[tuple[str, Timeframe]]
    ) -> AsyncGenerator[StreamEvent]:
        """Бесконечный поток закрытых свечей. Переподключается сам и сообщает об этом
        событием StreamReconnected."""

    @abstractmethod
    async def server_time_ms(self) -> int: ...

    def is_market_open(self, symbol: str, ts_ms: int) -> bool:
        """Крипта торгуется круглосуточно; адаптеры акций переопределяют."""
        return True

    # --- счёт и торговля ---
    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: Decimal) -> None: ...

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def amend_stops(
        self,
        symbol: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None: ...

    @abstractmethod
    async def close_position(self, symbol: str, qty: Decimal | None = None) -> OrderResult | None:
        """Закрывает позицию целиком (qty=None) или частично рыночным reduce-only ордером."""

    @abstractmethod
    async def cancel_all(self, symbol: str | None = None) -> None: ...

    async def aclose(self) -> None:  # noqa: B027 — необязательный хук
        """Освобождает сетевые ресурсы."""
