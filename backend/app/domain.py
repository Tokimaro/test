"""Доменные типы, общие для всех слоёв (брокеры, стратегия, риск, исполнение)."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum


class MarketType(StrEnum):
    CRYPTO = "crypto"
    STOCK = "stock"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class Timeframe(StrEnum):
    """Значения совпадают с параметром interval в Bybit V5."""

    M1 = "1"
    M5 = "5"
    M15 = "15"
    M30 = "30"
    H1 = "60"
    H4 = "240"
    D1 = "D"

    @property
    def seconds(self) -> int:
        return 86_400 if self is Timeframe.D1 else int(self.value) * 60

    @property
    def ms(self) -> int:
        return self.seconds * 1000


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int  # время открытия свечи, мс UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    market_type: MarketType
    category: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal = Decimal(0)
    max_leverage: Decimal = Decimal(1)
    taker_fee: Decimal = Decimal("0.00055")
    maker_fee: Decimal = Decimal("0.0002")

    def round_qty(self, qty: Decimal | float) -> Decimal:
        """Округляет объём ВНИЗ к шагу qty_step (никогда не увеличивает риск)."""
        q = Decimal(str(qty))
        if q <= 0:
            return Decimal(0)
        steps = (q / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        return (steps * self.qty_step).quantize(self.qty_step)

    def round_price(self, price: Decimal | float) -> Decimal:
        p = Decimal(str(price))
        steps = (p / self.tick_size).to_integral_value(rounding=ROUND_HALF_UP)
        return (steps * self.tick_size).quantize(self.tick_size)


@dataclass(frozen=True, slots=True)
class Balance:
    equity: Decimal
    available: Decimal
    currency: str = "USDT"


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    direction: Direction
    qty: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    unrealized_pnl: Decimal = Decimal(0)
    leverage: Decimal = Decimal(1)
    liq_price: Decimal | None = None
