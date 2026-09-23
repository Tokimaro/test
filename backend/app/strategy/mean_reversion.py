"""B. Возврат к среднему (раздел 5.4 плана): отскок от полосы Боллинджера во флэте."""

from app.strategy.base import Row, Strategy, SubSignal, clamp, isnan


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    W_BAND = 0.35
    W_RSI = 0.25
    W_DIVERGENCE = 0.25
    W_ENTRY_TF = 0.15

    def evaluate(self, row: Row) -> SubSignal:
        close, low, high = row["close"], row["low"], row["high"]
        upper, lower, rsi = row["bb_upper"], row["bb_lower"], row["rsi"]
        if isnan(close, upper, lower, rsi):
            return SubSignal(self.name, 0.0, {"skip": "warmup"})

        long: dict[str, float] = {}
        short: dict[str, float] = {}

        if low <= lower:
            long["band"] = self.W_BAND
        elif high >= upper:
            short["band"] = self.W_BAND
        else:
            return SubSignal(self.name, 0.0, {"band": "inside"})

        # RSI: полный вес при 20 и ниже (80 и выше), линейно от 30 (70)
        if "band" in long and rsi < 30:
            long["rsi"] = self.W_RSI * clamp((30 - rsi) / 10, 0, 1)
        if "band" in short and rsi > 70:
            short["rsi"] = self.W_RSI * clamp((rsi - 70) / 10, 0, 1)

        # Дивергенция: цена обновила последний свинг, а RSI — нет
        sw_low, rsi_sw_low = row["swing_low"], row["rsi_at_swing_low"]
        if "band" in long and not isnan(sw_low, rsi_sw_low) and low < sw_low and rsi > rsi_sw_low:
            long["divergence"] = self.W_DIVERGENCE
        sw_high, rsi_sw_high = row["swing_high"], row["rsi_at_swing_high"]
        if (
            "band" in short
            and not isnan(sw_high, rsi_sw_high)
            and high > sw_high
            and rsi < rsi_sw_high
        ):
            short["divergence"] = self.W_DIVERGENCE

        # Подтверждение разворота на младшем ТФ
        e_close, e_open = row["e_close"], row["e_open"]
        if not isnan(e_close, e_open):
            if "band" in long and e_close > e_open:
                long["entry_tf"] = self.W_ENTRY_TF
            if "band" in short and e_close < e_open:
                short["entry_tf"] = self.W_ENTRY_TF

        return SubSignal(self.name, self.combine(long, short), {"long": long, "short": short})
