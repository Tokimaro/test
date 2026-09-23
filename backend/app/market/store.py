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
