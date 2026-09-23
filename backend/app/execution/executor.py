"""Отправка ордеров с идемпотентностью и журналом в БД (раздел 7 плана).

Каждый ордер имеет клиентский orderLinkId, производный от id сделки. Если ответ биржи
потерян (сеть) или биржа сообщила о дубле, ордер ищется по link_id: так повторная отправка
никогда не откроет вторую позицию.
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog

from app.brokers.base import BrokerAdapter, BrokerError, OrderRequest, OrderResult, OrderType
from app.db.repo import TradeRepo
from app.domain import Direction, Instrument, Position

log = structlog.get_logger()

DUPLICATE_LINK_ID = 110072


class OrderUncertain(BrokerError):
    """Судьба ордера неизвестна (биржа недоступна) — решит сверка позиций."""


@dataclass(frozen=True, slots=True)
class EntryFill:
    entry_price: Decimal
    qty: Decimal
    tp1_link_id: str | None
    tp1_error: str | None = None  # вход состоялся, но TP1 выставить не удалось


def link_id(trade_id: int, purpose: str) -> str:
    return f"tb{trade_id}-{purpose}"


class OrderExecutor:
    def __init__(
        self,
        broker: BrokerAdapter,
        repo: TradeRepo,
        *,
        confirm_attempts: int = 10,
        confirm_delay_s: float = 0.5,
    ) -> None:
        self.broker = broker
        self.repo = repo
        self._attempts = confirm_attempts
        self._delay = confirm_delay_s

    async def submit(self, trade_id: int, purpose: str, req: OrderRequest) -> OrderResult:
        await self.repo.add_order(
            trade_id=trade_id,
            link_id=req.link_id,
            purpose=purpose,
            side=req.direction.value,
            order_type=req.order_type.value,
            qty=req.qty,
            price=req.price,
            status="submitting",
        )
        try:
            result = await self.broker.place_order(req)
        except BrokerError as exc:
            try:
                existing = await self.broker.get_order(req.symbol, req.link_id)
            except BrokerError as lookup_exc:
                await self.repo.update_order(req.link_id, "unknown")
                raise OrderUncertain(f"{req.link_id}: {exc}; проверка: {lookup_exc}") from exc
            if existing is None:
                if exc.code is None or exc.code == DUPLICATE_LINK_ID:
                    # ответ потерян (сеть) или биржа видит дубль, но ордер пока не находится:
                    # исход неизвестен — отказом это считать нельзя
                    await self.repo.update_order(req.link_id, "unknown")
                    raise OrderUncertain(f"{req.link_id}: {exc}") from exc
                await self.repo.update_order(req.link_id, "rejected")
                raise
            log.warning("executor.recovered_order", link_id=req.link_id, error=str(exc))
            result = existing
        await self.repo.update_order(req.link_id, result.status or "New", result.order_id)
        return result

    async def open_position(
        self,
        *,
        trade_id: int,
        instrument: Instrument,
        direction: Direction,
        qty: Decimal,
        leverage: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        tp1: Decimal | None,
        tp1_fraction: float,
        derivatives: bool,
    ) -> EntryFill:
        symbol = instrument.symbol
        if derivatives:
            await self.broker.set_leverage(symbol, leverage)
        tp1_qty = (
            instrument.round_qty(qty * Decimal(str(tp1_fraction)))
            if tp1 is not None and tp1_fraction > 0
            else Decimal(0)
        )
        split = (
            self.broker.supports_split_take_profit
            and tp1 is not None
            and instrument.min_qty <= tp1_qty < qty
        )
        await self.submit(
            trade_id,
            "entry",
            OrderRequest(
                symbol=symbol,
                direction=direction,
                qty=qty,
                link_id=link_id(trade_id, "entry"),
                stop_loss=stop,
                take_profit=take_profit,
                partial_take_profit=(tp1, tp1_qty) if split and tp1 is not None else None,
            ),
        )
        position = await self._await_position(symbol, direction)
        if split:
            # брокер уже выставил TP1 вместе с защитой
            return EntryFill(position.entry_price, position.qty, link_id(trade_id, "tp1"))
        if self.broker.supports_split_take_profit:
            tp1 = None  # отдельная лимитка TP1 у такого брокера заняла бы объём стопа
        # С этого момента позиция существует: любые ошибки ниже не должны её «потерять»
        tp1_link, tp1_error = None, None
        if tp1 is not None and tp1_fraction > 0:
            tp1_qty = instrument.round_qty(position.qty * Decimal(str(tp1_fraction)))
            if instrument.min_qty <= tp1_qty < position.qty:
                try:
                    await self.submit(
                        trade_id,
                        "tp1",
                        OrderRequest(
                            symbol=symbol,
                            direction=direction.opposite,
                            qty=tp1_qty,
                            link_id=link_id(trade_id, "tp1"),
                            order_type=OrderType.LIMIT,
                            price=tp1,
                            reduce_only=True,
                        ),
                    )
                    tp1_link = link_id(trade_id, "tp1")
                except BrokerError as exc:
                    log.error("executor.tp1_failed", symbol=symbol, error=str(exc))
                    tp1_error = str(exc)
        return EntryFill(position.entry_price, position.qty, tp1_link, tp1_error)

    async def _await_position(self, symbol: str, direction: Direction) -> Position:
        """Позиция появляется на бирже не мгновенно — ждём подтверждения."""
        for attempt in range(self._attempts):
            for p in await self.broker.get_positions():
                if p.symbol == symbol and p.direction is direction and p.qty > 0:
                    return p
            if attempt + 1 < self._attempts:
                await asyncio.sleep(self._delay)
        raise OrderUncertain(f"{symbol}: позиция не появилась после входа")

    async def close_position(self, trade_id: int, symbol: str) -> None:
        await self.broker.cancel_all(symbol)
        await self.broker.close_position(symbol)
        log.info("executor.closed", trade_id=trade_id, symbol=symbol)
