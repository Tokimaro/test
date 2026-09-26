"""Paper-брокер: рыночные данные — от настоящего адаптера, исполнение — локальная симуляция.

Позволяет гонять бота без ключей API. Исполнение консервативное, как в бэктесте:
если в одной свече задеты и стоп, и цель — срабатывает стоп. Состояние сериализуется,
чтобы переживать рестарт (иначе после перезапуска «пропадали» бы позиции).
"""

import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    ClosedPnl,
    OrderRequest,
    OrderResult,
    OrderType,
    StreamEvent,
)
from app.domain import Balance, Candle, Direction, Instrument, MarketType, Position, Timeframe

DUPLICATE_LINK_ID = 110072  # как у Bybit: повтор orderLinkId
CLOSED_HISTORY = 500  # сколько последних закрытий хранить в сериализованном состоянии


@dataclass
class _Pos:
    direction: Direction
    qty: Decimal
    entry: Decimal
    stop: Decimal | None = None
    take_profit: Decimal | None = None
    open_fees: Decimal = Decimal(0)  # комиссия входа, ещё не отнесённая на закрытия
    opened_at: int = 0  # мс; свечи, закончившиеся до входа, не могут задеть стоп


@dataclass
class _Limit:
    link_id: str
    symbol: str
    direction: Direction
    qty: Decimal
    price: Decimal
    reduce_only: bool


class PaperBroker(BrokerAdapter):
    name = "paper"

    def __init__(
        self,
        data: BrokerAdapter,
        *,
        initial_equity: Decimal = Decimal(10_000),
        slippage_pct: Decimal = Decimal("0.0005"),
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._data = data
        self._clock = clock
        self._slippage = slippage_pct
        self.cash = initial_equity
        self.positions: dict[str, _Pos] = {}
        self.leverage: dict[str, Decimal] = {}
        self.limits: dict[str, _Limit] = {}
        self.orders: dict[str, OrderResult] = {}
        self.closed: list[ClosedPnl] = []
        self.last_price: dict[str, Decimal] = {}
        self.last_price_ts: dict[str, int] = {}
        self.last_candle_end: dict[str, int] = {}
        self._seq = 0

    # ------------------------------------------------------------------ данные
    def market_type(self) -> MarketType:
        return self._data.market_type()

    async def get_instrument(self, symbol: str) -> Instrument:
        return await self._data.get_instrument(symbol)

    async def get_candles(
        self, symbol: str, timeframe: Timeframe, start_ms: int, end_ms: int
    ) -> list[Candle]:
        return await self._data.get_candles(symbol, timeframe, start_ms, end_ms)

    def stream_candles(
        self, subscriptions: list[tuple[str, Timeframe]]
    ) -> AsyncGenerator[StreamEvent]:
        return self._data.stream_candles(subscriptions)

    async def server_time_ms(self) -> int:
        return await self._data.server_time_ms()

    async def get_funding_rate(self, symbol: str) -> float | None:
        return await self._data.get_funding_rate(symbol)

    def is_market_open(self, symbol: str, ts_ms: int) -> bool:
        return self._data.is_market_open(symbol, ts_ms)

    async def aclose(self) -> None:
        await self._data.aclose()

    # ------------------------------------------------------------------ счёт
    async def get_balance(self) -> Balance:
        unreal = sum(
            (self.last_price.get(s, p.entry) - p.entry) * p.direction.sign * p.qty
            for s, p in self.positions.items()
        )
        equity = self.cash + unreal
        margin = sum(
            p.entry * p.qty / self.leverage.get(s, Decimal(1)) for s, p in self.positions.items()
        )
        return Balance(equity=equity, available=max(Decimal(0), equity - margin))

    async def get_positions(self) -> list[Position]:
        out = []
        for s, p in self.positions.items():
            price = self.last_price.get(s, p.entry)
            out.append(
                Position(
                    symbol=s,
                    direction=p.direction,
                    qty=p.qty,
                    entry_price=p.entry,
                    stop_loss=p.stop,
                    take_profit=p.take_profit,
                    unrealized_pnl=(price - p.entry) * p.direction.sign * p.qty,
                    leverage=self.leverage.get(s, Decimal(1)),
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self.leverage[symbol] = leverage

    # ------------------------------------------------------------------ ордера
    async def place_order(self, req: OrderRequest) -> OrderResult:
        if req.link_id in self.orders:
            raise BrokerError("OrderLinkedID is duplicate", code=DUPLICATE_LINK_ID)
        if req.qty <= 0:
            raise BrokerError("qty должен быть > 0")
        inst = await self.get_instrument(req.symbol)
        if req.order_type is OrderType.LIMIT:
            if req.price is None:
                raise BrokerError("для лимитного ордера нужна цена")
            self.limits[req.link_id] = _Limit(
                req.link_id, req.symbol, req.direction, req.qty, req.price, req.reduce_only
            )
            result = OrderResult(self._next_id(), req.link_id, status="New")
            self.orders[req.link_id] = result
            return result

        price = self.last_price.get(req.symbol)
        if price is None:
            raise BrokerError(f"нет цены для {req.symbol}")
        fill = price * (1 + req.direction.sign * self._slippage)
        filled = self._execute(
            inst,
            req.symbol,
            req.direction,
            req.qty,
            fill,
            inst.taker_fee,
            reduce_only=req.reduce_only,
        )
        pos = self.positions.get(req.symbol)
        if pos is not None and not req.reduce_only:
            if req.stop_loss is not None:
                pos.stop = req.stop_loss
            if req.take_profit is not None:
                pos.take_profit = req.take_profit
        result = OrderResult(
            self._next_id(), req.link_id, status="Filled", avg_price=fill, filled_qty=filled
        )
        self.orders[req.link_id] = result
        return result

    async def get_order(self, symbol: str, link_id: str) -> OrderResult | None:
        return self.orders.get(link_id)

    async def get_closed_pnl(self, symbol: str, since_ms: int) -> list[ClosedPnl]:
        return [c for c in self.closed if c.symbol == symbol and c.ts >= since_ms]

    async def amend_stops(
        self,
        symbol: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            raise BrokerError(f"нет позиции {symbol}")
        if stop_loss is not None:
            pos.stop = stop_loss
        if take_profit is not None:
            pos.take_profit = take_profit

    async def close_position(self, symbol: str, qty: Decimal | None = None) -> OrderResult | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        close_qty = pos.qty if qty is None else min(qty, pos.qty)
        return await self.place_order(
            OrderRequest(
                symbol=symbol,
                direction=pos.direction.opposite,
                qty=close_qty,
                link_id=f"paper-close-{self._next_id()}",
                reduce_only=True,
            )
        )

    async def cancel_order(self, symbol: str, link_id: str) -> bool:
        lim = self.limits.pop(link_id, None)
        if lim is None:
            return False
        self.orders[link_id] = OrderResult(self.orders[link_id].order_id, link_id, "Cancelled")
        return True

    async def cancel_all(self, symbol: str | None = None) -> None:
        for link_id, lim in list(self.limits.items()):
            if symbol is None or lim.symbol == symbol:
                del self.limits[link_id]
                self.orders[link_id] = OrderResult(
                    self.orders[link_id].order_id, link_id, status="Cancelled"
                )

    # ------------------------------------------------------------------ симуляция
    def mark_price(self, symbol: str, price: float, ts: int) -> None:
        """Обновляет последнюю цену без проверки стопов (например, закрытие рабочей свечи)."""
        if ts >= self.last_price_ts.get(symbol, -1):
            self.last_price[symbol] = Decimal(str(price))
            self.last_price_ts[symbol] = ts

    async def on_candle(self, symbol: str, candle: Candle, tf_ms: int = 0) -> None:
        """Проверяет срабатывание стопов/целей/лимиток по свече и обновляет цену.

        Идемпотентно: свеча, уже обработанная (по времени окончания), игнорируется —
        поэтому докачанные после обрыва свечи можно безопасно «проигрывать» повторно.
        """
        end = candle.ts + tf_ms
        if end <= self.last_candle_end.get(symbol, -1):
            return
        self.last_candle_end[symbol] = end
        high, low = Decimal(str(candle.high)), Decimal(str(candle.low))
        o = Decimal(str(candle.open))
        pos = self.positions.get(symbol)
        if pos is not None and pos.opened_at >= end:
            pos = None  # свеча целиком до входа — её экстремумы к позиции не относятся
        inst = await self.get_instrument(symbol) if pos is not None else None
        if pos is not None and inst is not None:
            sign = pos.direction.sign
            if pos.stop is not None and (low <= pos.stop if sign > 0 else high >= pos.stop):
                # гэп через стоп — по open, иначе по цене стопа; проскальзывание против нас
                base = o if (o - pos.stop) * sign <= 0 else pos.stop
                fill = base * (1 - sign * self._slippage)
                self._execute(
                    inst,
                    symbol,
                    pos.direction.opposite,
                    pos.qty,
                    fill,
                    inst.taker_fee,
                    reduce_only=True,
                )
                self._drop_reduce_limits(symbol)
            else:
                for lim in self._limits_for(symbol):
                    touched = (
                        high >= lim.price
                        if lim.direction is Direction.SHORT
                        else (low <= lim.price)
                    )
                    if touched and symbol in self.positions:
                        del self.limits[lim.link_id]
                        filled = self._execute(
                            inst,
                            symbol,
                            lim.direction,
                            lim.qty,
                            lim.price,
                            inst.maker_fee,
                            reduce_only=lim.reduce_only,
                        )
                        self.orders[lim.link_id] = OrderResult(
                            self.orders[lim.link_id].order_id,
                            lim.link_id,
                            status="Filled",
                            avg_price=lim.price,
                            filled_qty=filled,
                        )
                pos = self.positions.get(symbol)
                if (
                    pos is not None
                    and pos.take_profit is not None
                    and (high >= pos.take_profit if sign > 0 else low <= pos.take_profit)
                ):
                    fill = pos.take_profit * (1 - sign * self._slippage)
                    self._execute(
                        inst,
                        symbol,
                        pos.direction.opposite,
                        pos.qty,
                        fill,
                        inst.taker_fee,
                        reduce_only=True,
                    )
                    self._drop_reduce_limits(symbol)
        self.mark_price(symbol, candle.close, end)

    def _limits_for(self, symbol: str) -> list[_Limit]:
        return [lim for lim in self.limits.values() if lim.symbol == symbol]

    def _drop_reduce_limits(self, symbol: str) -> None:
        if symbol not in self.positions:
            for lim in self._limits_for(symbol):
                if lim.reduce_only:
                    del self.limits[lim.link_id]
                    self.orders[lim.link_id] = OrderResult(
                        self.orders[lim.link_id].order_id, lim.link_id, status="Cancelled"
                    )

    def _execute(
        self,
        inst: Instrument,
        symbol: str,
        direction: Direction,
        qty: Decimal,
        price: Decimal,
        fee_rate: Decimal,
        *,
        reduce_only: bool,
    ) -> Decimal:
        pos = self.positions.get(symbol)
        if pos is None or pos.direction is direction:
            if reduce_only:
                raise BrokerError("reduce-only ордер без позиции")
            fee = price * qty * fee_rate
            if pos is None:
                self.positions[symbol] = _Pos(
                    direction, qty, price, open_fees=fee, opened_at=self._clock()
                )
            else:
                total = pos.qty + qty
                pos.entry = (pos.entry * pos.qty + price * qty) / total
                pos.qty = total
                pos.open_fees += fee
            self.cash -= fee
            return qty
        # уменьшение позиции
        close_qty = min(qty, pos.qty)
        fee = price * close_qty * fee_rate
        gross = (price - pos.entry) * pos.direction.sign * close_qty
        self.cash += gross - fee
        # как closedPnl у Bybit: за вычетом комиссий входа (пропорционально) и выхода
        entry_fee_share = pos.open_fees * close_qty / pos.qty
        pos.open_fees -= entry_fee_share
        pnl = gross - fee - entry_fee_share
        pos.qty -= close_qty
        self.closed.append(
            ClosedPnl(
                symbol=symbol,
                qty=close_qty,
                avg_entry=pos.entry,
                avg_exit=price,
                pnl=pnl,
                ts=self._clock(),
            )
        )
        if pos.qty <= 0:
            del self.positions[symbol]
        return close_qty

    def _next_id(self) -> str:
        self._seq += 1
        return f"P{self._seq}"

    # ------------------------------------------------------------------ состояние
    def to_dict(self) -> dict[str, Any]:
        return {
            "cash": str(self.cash),
            "seq": self._seq,
            "positions": {
                s: {
                    "direction": p.direction.value,
                    "qty": str(p.qty),
                    "entry": str(p.entry),
                    "stop": str(p.stop) if p.stop is not None else None,
                    "take_profit": str(p.take_profit) if p.take_profit is not None else None,
                    "open_fees": str(p.open_fees),
                    "opened_at": p.opened_at,
                }
                for s, p in self.positions.items()
            },
            "leverage": {s: str(v) for s, v in self.leverage.items()},
            "limits": {
                k: {
                    "symbol": v.symbol,
                    "direction": v.direction.value,
                    "qty": str(v.qty),
                    "price": str(v.price),
                    "reduce_only": v.reduce_only,
                }
                for k, v in self.limits.items()
            },
            "order_ids": {k: v.order_id for k, v in self.orders.items()},
            "order_status": {k: v.status for k, v in self.orders.items()},
            "order_fills": {
                k: [str(v.avg_price), str(v.filled_qty)]
                for k, v in self.orders.items()
                if v.avg_price is not None
            },
            "closed": [
                {
                    "symbol": c.symbol,
                    "qty": str(c.qty),
                    "avg_entry": str(c.avg_entry),
                    "avg_exit": str(c.avg_exit),
                    "pnl": str(c.pnl),
                    "ts": c.ts,
                }
                for c in self.closed[-CLOSED_HISTORY:]
            ],
            "last_price": {s: str(v) for s, v in self.last_price.items()},
            "last_price_ts": dict(self.last_price_ts),
            "last_candle_end": dict(self.last_candle_end),
        }

    def load_dict(self, d: dict[str, Any]) -> None:
        def dec(v: Any) -> Decimal | None:
            return Decimal(v) if v is not None else None

        self.cash = Decimal(d["cash"])
        self._seq = int(d.get("seq", 0))
        self.positions = {
            s: _Pos(
                Direction(p["direction"]),
                Decimal(p["qty"]),
                Decimal(p["entry"]),
                dec(p.get("stop")),
                dec(p.get("take_profit")),
                Decimal(p.get("open_fees", "0")),
                int(p.get("opened_at", 0)),
            )
            for s, p in d.get("positions", {}).items()
        }
        self.leverage = {s: Decimal(v) for s, v in d.get("leverage", {}).items()}
        self.limits = {
            k: _Limit(
                k,
                v["symbol"],
                Direction(v["direction"]),
                Decimal(v["qty"]),
                Decimal(v["price"]),
                bool(v["reduce_only"]),
            )
            for k, v in d.get("limits", {}).items()
        }
        statuses = d.get("order_status", {})
        fills = d.get("order_fills", {})
        self.orders = {
            k: OrderResult(
                oid,
                k,
                status="New" if k in self.limits else statuses.get(k, "Filled"),
                avg_price=Decimal(fills[k][0]) if k in fills else None,
                filled_qty=Decimal(fills[k][1]) if k in fills else Decimal(0),
            )
            for k, oid in d.get("order_ids", {}).items()
        }
        self.closed = [
            ClosedPnl(
                symbol=c["symbol"],
                qty=Decimal(c["qty"]),
                avg_entry=Decimal(c["avg_entry"]),
                avg_exit=Decimal(c["avg_exit"]),
                pnl=Decimal(c["pnl"]),
                ts=int(c["ts"]),
            )
            for c in d.get("closed", [])
        ]
        self.last_price = {s: Decimal(v) for s, v in d.get("last_price", {}).items()}
        self.last_price_ts = {s: int(v) for s, v in d.get("last_price_ts", {}).items()}
        self.last_candle_end = {s: int(v) for s, v in d.get("last_candle_end", {}).items()}
