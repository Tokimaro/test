"""Репозиторий торговых данных: сигналы, сделки, ордера, капитал, события риска, состояние."""

from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.candles import ms_to_dt
from app.db.models import (
    EquitySnapshotRow,
    OrderRow,
    RiskEventRow,
    RuntimeStateRow,
    SettingRow,
    SignalRow,
    TradeRow,
)
from app.risk.manager import RiskEvent
from app.strategy.trend import Signal


def _jsonable(value: Any) -> Any:
    """Приводит numpy/Decimal/Enum к типам, которые можно сохранить в JSON."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return str(value)


class TradeRepo:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], mode: str) -> None:
        self._sm = sessionmaker
        self.mode = mode

    # ------------------------------------------------------------------ сигналы
    async def save_signal(
        self, instrument_id: int, signal: Signal, acted: bool, reject_reason: str | None
    ) -> int:
        async with self._sm() as s, s.begin():
            row = SignalRow(
                ts=ms_to_dt(signal.ts),
                instrument_id=instrument_id,
                mode=self.mode,
                direction=signal.direction,
                # для стратегии тренда: целевая доля монеты в портфеле, %
                confidence=round(signal.weight * 100, 4),
                regime="rebalance" if acted else "",
                components=_jsonable(
                    {**signal.components, "weight": signal.weight, "score": signal.score}
                ),
                acted=acted,
                reject_reason=reject_reason,
            )
            s.add(row)
            await s.flush()
            return int(row.id)

    async def mark_signal(self, signal_id: int, acted: bool, reject_reason: str | None) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(
                update(SignalRow)
                .where(SignalRow.id == signal_id)
                .values(acted=acted, reject_reason=reject_reason)
            )

    # ------------------------------------------------------------------ сделки
    async def create_trade(self, **fields: Any) -> int:
        async with self._sm() as s, s.begin():
            row = TradeRow(mode=self.mode, **fields)
            s.add(row)
            await s.flush()
            return int(row.id)

    async def update_trade(self, trade_id: int, **fields: Any) -> None:
        async with self._sm() as s, s.begin():
            await s.execute(update(TradeRow).where(TradeRow.id == trade_id).values(**fields))

    async def get_trade(self, trade_id: int) -> TradeRow | None:
        async with self._sm() as s:
            return await s.get(TradeRow, trade_id)

    async def open_trades(self) -> list[TradeRow]:
        async with self._sm() as s:
            q = select(TradeRow).where(
                TradeRow.mode == self.mode, TradeRow.status.in_(("pending", "open"))
            )
            return list((await s.scalars(q)).all())

    # ------------------------------------------------------------------ ордера
    async def add_order(
        self,
        *,
        trade_id: int,
        link_id: str,
        purpose: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        status: str,
    ) -> None:
        async with self._sm() as s, s.begin():
            s.add(
                OrderRow(
                    trade_id=trade_id,
                    link_id=link_id,
                    purpose=purpose,
                    side=side,
                    order_type=order_type,
                    qty=qty,
                    price=price,
                    status=status,
                )
            )

    async def update_order(
        self, link_id: str, status: str, exchange_order_id: str | None = None
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if exchange_order_id:
            values["exchange_order_id"] = exchange_order_id
        async with self._sm() as s, s.begin():
            await s.execute(update(OrderRow).where(OrderRow.link_id == link_id).values(**values))

    async def orders_for(self, trade_id: int) -> list[OrderRow]:
        async with self._sm() as s:
            q = select(OrderRow).where(OrderRow.trade_id == trade_id).order_by(OrderRow.id)
            return list((await s.scalars(q)).all())

    # ------------------------------------------------------------------ капитал и риск
    async def save_equity(
        self,
        ts_ms: int,
        equity: Decimal,
        balance: Decimal,
        unrealized: Decimal,
        open_risk: Decimal,
    ) -> None:
        values = {
            "ts": ms_to_dt(ts_ms),
            "mode": self.mode,
            "equity": equity,
            "balance": balance,
            "unrealized_pnl": unrealized,
            "open_risk": open_risk,
        }
        stmt = insert(EquitySnapshotRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["ts", "mode"],
            set_={
                k: stmt.excluded[k] for k in ("equity", "balance", "unrealized_pnl", "open_risk")
            },
        )
        async with self._sm() as s, s.begin():
            await s.execute(stmt)

    async def save_risk_event(self, event: RiskEvent) -> None:
        async with self._sm() as s, s.begin():
            s.add(
                RiskEventRow(
                    ts=ms_to_dt(event.ts),
                    type=event.type,
                    details=_jsonable({**event.details, "mode": self.mode}),
                )
            )

    # ------------------------------------------------------------------ состояние/настройки
    async def get_state(self, key: str) -> dict[str, Any] | None:
        async with self._sm() as s:
            row = await s.get(RuntimeStateRow, f"{self.mode}:{key}")
            return dict(row.value) if row is not None else None

    async def set_state(self, key: str, value: dict[str, Any]) -> None:
        stmt = insert(RuntimeStateRow).values(key=f"{self.mode}:{key}", value=_jsonable(value))
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": func.now()}
        )
        async with self._sm() as s, s.begin():
            await s.execute(stmt)

    async def get_setting(self, key: str) -> dict[str, Any] | None:
        async with self._sm() as s:
            row = await s.scalar(
                select(SettingRow)
                .where(SettingRow.key == key)
                .order_by(desc(SettingRow.id))
                .limit(1)
            )
            return dict(row.value) if row is not None else None

    async def set_setting(self, key: str, value: dict[str, Any], updated_by: str | None) -> None:
        async with self._sm() as s, s.begin():
            s.add(SettingRow(key=key, value=_jsonable(value), updated_by=updated_by))
