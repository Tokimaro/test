"""Отправка ордеров с идемпотентностью и журналом в БД.

Каждый ордер имеет клиентский orderLinkId, производный от id сделки. Если ответ биржи
потерян (сеть) или биржа сообщила о дубле, ордер ищется по link_id: так повторная отправка
никогда не откроет вторую позицию.
"""

import asyncio
from dataclasses import dataclass
from decimal import Decimal

import structlog

from app.brokers.base import BrokerAdapter, BrokerError, OrderRequest, OrderResult
from app.db.repo import TradeRepo
from app.domain import Direction

log = structlog.get_logger()

DUPLICATE_LINK_ID = 110072


class OrderUncertain(BrokerError):
    """Судьба ордера неизвестна (биржа недоступна) — решит сверка позиций."""


FILLED = {"Filled"}
DEAD = {"Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}


@dataclass(frozen=True, slots=True)
class Fill:
    price: Decimal
    qty: Decimal


def link_id(trade_id: int, purpose: str) -> str:
    return f"tb{trade_id}-{purpose}"


class OrderExecutor:
    def __init__(
        self,
        broker: BrokerAdapter,
        repo: TradeRepo,
        *,
        fill_attempts: int = 10,
        fill_delay_s: float = 0.5,
    ) -> None:
        self.broker = broker
        self.repo = repo
        self._attempts = fill_attempts
        self._delay = fill_delay_s

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

    async def market_order(
        self,
        *,
        trade_id: int,
        purpose: str,
        symbol: str,
        direction: Direction,
        qty: Decimal,
        reduce_only: bool = False,
    ) -> Fill:
        """Рыночный ордер с ожиданием исполнения. Возвращает фактические цену и объём."""
        req = OrderRequest(
            symbol=symbol,
            direction=direction,
            qty=qty,
            link_id=link_id(trade_id, purpose),
            reduce_only=reduce_only,
        )
        result = await self.submit(trade_id, purpose, req)
        for attempt in range(self._attempts):
            if result.status in FILLED and result.avg_price and result.filled_qty > 0:
                await self.repo.update_order(req.link_id, result.status, result.order_id)
                return Fill(result.avg_price, result.filled_qty)
            if result.status in DEAD:
                if result.avg_price and result.filled_qty > 0:  # исполнился частично
                    return Fill(result.avg_price, result.filled_qty)
                await self.repo.update_order(req.link_id, result.status, result.order_id)
                raise BrokerError(f"{req.link_id}: ордер {result.status}")
            if attempt:
                await asyncio.sleep(self._delay)
            try:
                found = await self.broker.get_order(symbol, req.link_id)
            except BrokerError as exc:
                log.warning("executor.order_lookup_failed", link_id=req.link_id, error=str(exc))
                continue
            if found is not None:
                result = found
        await self.repo.update_order(req.link_id, "unknown")
        raise OrderUncertain(f"{req.link_id}: исполнение не подтвердилось")
