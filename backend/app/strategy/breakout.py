"""C. Пробой после сжатия волатильности (раздел 5.4 плана)."""

import numpy as np

from app.strategy.base import Row, Strategy, SubSignal, isnan


class BreakoutStrategy(Strategy):
    name = "breakout"

    W_SQUEEZE = 0.30
    W_BREAK = 0.40
    W_VOLUME = 0.30
    SQUEEZE_PCT = 10.0  # ширина Боллинджера в нижних 10% за 120 свечей
    SQUEEZE_LOOKBACK = 10  # сжатие должно быть недавно, а не обязательно на текущей свече
    VOLUME_MULT = 1.5

    def evaluate(self, row: Row) -> SubSignal:
        close, don_up, don_low = row["close"], row["don_upper"], row["don_lower"]
        if isnan(close, don_up, don_low):
            return SubSignal(self.name, 0.0, {"skip": "warmup"})

        if close > don_up:
            side = 1
        elif close < don_low:
            side = -1
        else:
            return SubSignal(self.name, 0.0, {"break": "none"})

        parts: dict[str, float] = {"break": self.W_BREAK}
        widths = row.window("bb_width_pct", self.SQUEEZE_LOOKBACK)
        widths = widths[~np.isnan(widths)]
        if widths.size and float(widths.min()) <= self.SQUEEZE_PCT:
            parts["squeeze"] = self.W_SQUEEZE
        vol, vol_sma = row["volume"], row["vol_sma"]
        if not isnan(vol, vol_sma) and vol > self.VOLUME_MULT * vol_sma:
            parts["volume"] = self.W_VOLUME
        # Пробой без сжатия и без объёма — чаще всего ложный
        if len(parts) == 1:
            return SubSignal(self.name, 0.0, {"break": "unconfirmed"})
        score = side * sum(parts.values())
        return SubSignal(self.name, score, {"long" if side > 0 else "short": parts})
