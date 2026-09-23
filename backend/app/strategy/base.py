"""Базовые типы стратегий.

Каждая под-стратегия возвращает оценку score ∈ [-1; +1] (плюс — лонг, минус — шорт)
и словарь причин, который сохраняется в БД и показывается в панели.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.regime import Regime


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Внешние данные, не выводимые из свечей инструмента."""

    funding_rate: float | None = None  # ставка финансирования за 8ч (0.0001 = 0.01%)
    btc_regime: Regime | None = None  # режим BTC — для фильтра альткоинов
    is_btc: bool = False


class Row:
    """Удобный доступ к строке признаков и предыдущим строкам без копирования DataFrame."""

    __slots__ = ("_cols", "_i")

    def __init__(self, cols: dict[str, np.ndarray], i: int) -> None:
        self._cols = cols
        self._i = i

    def __getitem__(self, key: str) -> float:
        return float(self._cols[key][self._i])

    def prev(self, key: str, back: int = 1) -> float:
        j = self._i - back
        return float(self._cols[key][j]) if j >= 0 else float("nan")

    def window(self, key: str, size: int) -> np.ndarray:
        start = max(0, self._i - size + 1)
        return self._cols[key][start : self._i + 1]

    def get_str(self, key: str) -> str:
        return str(self._cols[key][self._i])


def columns_of(df: pd.DataFrame) -> dict[str, np.ndarray]:
    return {c: df[c].to_numpy() for c in df.columns}


@dataclass(frozen=True, slots=True)
class SubSignal:
    name: str
    score: float
    reasons: dict[str, Any] = field(default_factory=dict)


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def isnan(*values: float) -> bool:
    return any(v != v for v in values)  # NaN != NaN


class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, row: Row) -> SubSignal: ...

    @staticmethod
    def combine(long_parts: dict[str, float], short_parts: dict[str, float]) -> float:
        return clamp(sum(long_parts.values()) - sum(short_parts.values()))
