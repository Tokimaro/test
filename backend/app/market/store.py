from typing import Protocol

from app.domain import Candle, Timeframe


class CandleStore(Protocol):
    async def save_candles(
        self, symbol: str, timeframe: Timeframe, candles: list[Candle]
    ) -> None: ...

    async def last_ts(self, symbol: str, timeframe: Timeframe) -> int | None: ...

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Свечи по возрастанию времени. С limit — последние limit свечей диапазона."""
        ...


class RoutingCandleStore:
    """Направляет запросы в хранилище рынка, которому принадлежит символ."""

    def __init__(self, routes: dict[str, CandleStore]) -> None:
        self._routes = routes

    def _store(self, symbol: str) -> CandleStore:
        try:
            return self._routes[symbol]
        except KeyError:
            raise KeyError(f"символ {symbol} не относится ни к одному рынку") from None

    async def save_candles(self, symbol: str, timeframe: Timeframe, candles: list[Candle]) -> None:
        await self._store(symbol).save_candles(symbol, timeframe, candles)

    async def last_ts(self, symbol: str, timeframe: Timeframe) -> int | None:
        return await self._store(symbol).last_ts(symbol, timeframe)

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        return await self._store(symbol).get_candles(symbol, timeframe, start_ms, end_ms, limit)


class InMemoryCandleStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, Timeframe], dict[int, Candle]] = {}

    async def save_candles(self, symbol: str, timeframe: Timeframe, candles: list[Candle]) -> None:
        bucket = self._data.setdefault((symbol, timeframe), {})
        for c in candles:
            bucket[c.ts] = c

    async def last_ts(self, symbol: str, timeframe: Timeframe) -> int | None:
        bucket = self._data.get((symbol, timeframe))
        return max(bucket) if bucket else None

    async def get_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        bucket = self._data.get((symbol, timeframe), {})
        rows = [
            bucket[ts]
            for ts in sorted(bucket)
            if (start_ms is None or ts >= start_ms) and (end_ms is None or ts <= end_ms)
        ]
        return rows[-limit:] if limit else rows
