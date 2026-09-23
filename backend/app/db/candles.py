from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import CandleRow, InstrumentRow
from app.domain import Candle, Instrument, Timeframe

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


def ms_to_dt(ms: int) -> datetime:
    return EPOCH + ms * _MS


def dt_to_ms(dt: datetime) -> int:
    """Точная (без float) конвертация в миллисекунды."""
    return (dt - EPOCH) // _MS


async def upsert_instrument(session: AsyncSession, broker: str, inst: Instrument) -> int:
    values = {
        "broker": broker,
        "symbol": inst.symbol,
        "market_type": inst.market_type.value,
        "category": inst.category,
        "tick_size": inst.tick_size,
        "qty_step": inst.qty_step,
        "min_qty": inst.min_qty,
        "max_qty": inst.max_qty,
        "min_notional": inst.min_notional,
        "max_leverage": inst.max_leverage,
        "taker_fee": inst.taker_fee,
        "maker_fee": inst.maker_fee,
    }
    ins = insert(InstrumentRow).values(**values)
    stmt = ins.on_conflict_do_update(
        constraint="uq_instruments_broker_symbol",
        set_={k: ins.excluded[k] for k in values if k not in ("broker", "symbol")}
        | {"updated_at": func.now()},
    ).returning(InstrumentRow.id)
    return int((await session.execute(stmt)).scalar_one())


class SqlCandleStore:
    """CandleStore поверх PostgreSQL. Инструменты должны быть зарегистрированы заранее."""

    # asyncpg ограничивает число параметров запроса (32767); 8 колонок на строку
    BATCH = 2000

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], broker: str) -> None:
        self._sm = sessionmaker
        self._broker = broker
        self._ids: dict[str, int] = {}

    async def register(self, inst: Instrument) -> int:
        async with self._sm() as session, session.begin():
            iid = await upsert_instrument(session, self._broker, inst)
        self._ids[inst.symbol] = iid
        return iid

    async def _id(self, symbol: str) -> int:
        if symbol not in self._ids:
            async with self._sm() as session:
                iid = await session.scalar(
                    select(InstrumentRow.id).where(
                        InstrumentRow.broker == self._broker, InstrumentRow.symbol == symbol
                    )
                )
            if iid is None:
                raise KeyError(f"инструмент {self._broker}:{symbol} не зарегистрирован")
            self._ids[symbol] = iid
        return self._ids[symbol]

    async def save_candles(self, symbol: str, timeframe: Timeframe, candles: list[Candle]) -> None:
        if not candles:
            return
        iid = await self._id(symbol)
        async with self._sm() as session, session.begin():
            for i in range(0, len(candles), self.BATCH):
                rows = [
                    {
                        "instrument_id": iid,
                        "tf": timeframe.value,
                        "ts": ms_to_dt(c.ts),
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                        "turnover": c.turnover,
                    }
                    for c in candles[i : i + self.BATCH]
                ]
                stmt = insert(CandleRow).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["instrument_id", "tf", "ts"],
                    set_={
                        k: stmt.excluded[k]
                        for k in ("open", "high", "low", "close", "volume", "turnover")
                    },
                )
                await session.execute(stmt)

    async def last_ts(self, symbol: str, timeframe: Timeframe) -> int | None:
        iid = await self._id(symbol)
        async with self._sm() as session:
            ts = await session.scalar(
                select(func.max(CandleRow.ts)).where(
                    CandleRow.instrument_id == iid, CandleRow.tf == timeframe.value
                )
            )
        return dt_to_ms(ts) if ts is not None else None

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        iid = await self._id(symbol)
        q = select(CandleRow).where(CandleRow.instrument_id == iid, CandleRow.tf == timeframe.value)
        if start_ms is not None:
            q = q.where(CandleRow.ts >= ms_to_dt(start_ms))
        if end_ms is not None:
            q = q.where(CandleRow.ts <= ms_to_dt(end_ms))
        q = q.order_by(CandleRow.ts.desc() if limit else CandleRow.ts)
        if limit:
            q = q.limit(limit)
        async with self._sm() as session:
            rows = list((await session.scalars(q)).all())
        if limit:
            rows.reverse()
        return [
            Candle(
                ts=dt_to_ms(r.ts),
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                turnover=r.turnover,
            )
            for r in rows
        ]
