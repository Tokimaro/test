"""A. Следование тренду (раздел 5.4 плана): вход на откате к EMA21/VWAP по направлению тренда."""

from app.strategy.base import Row, Strategy, SubSignal, isnan


class TrendStrategy(Strategy):
    name = "trend"

    # веса компонентов, сумма = 1.0 — идеальный сетап даёт score = ±1
    W_STRUCTURE = 0.35
    W_PULLBACK = 0.25
    W_MACD = 0.20
    W_VOLUME = 0.10
    W_ENTRY_TF = 0.10

    def evaluate(self, row: Row) -> SubSignal:
        close, ema21, ema55 = row["close"], row["ema21"], row["ema55"]
        s21, s55 = row["ema21_slope"], row["ema55_slope"]
        atr = row["atr"]
        if isnan(close, ema21, ema55, s21, s55, atr) or atr <= 0:
            return SubSignal(self.name, 0.0, {"skip": "warmup"})

        long: dict[str, float] = {}
        short: dict[str, float] = {}

        if ema21 > ema55 and s21 > 0 and s55 > 0:
            long["structure"] = self.W_STRUCTURE
        elif ema21 < ema55 and s21 < 0 and s55 < 0:
            short["structure"] = self.W_STRUCTURE
        else:
            # без трендовой структуры остальные признаки не имеют смысла
            return SubSignal(self.name, 0.0, {"structure": "none"})

        # Откат: минимум текущей/предыдущей свечи коснулся зоны EMA21/VWAP (±0.3 ATR),
        # а закрытие осталось по тренду.
        vwap = row["vwap"]
        zone = [ema21] + ([vwap] if not isnan(vwap) else [])
        low = min(row["low"], row.prev("low"))
        high = max(row["high"], row.prev("high"))
        if "structure" in long and close > ema21 and any(low <= z + 0.3 * atr for z in zone):
            long["pullback"] = self.W_PULLBACK
        if "structure" in short and close < ema21 and any(high >= z - 0.3 * atr for z in zone):
            short["pullback"] = self.W_PULLBACK

        hist, prev_hist = row["macd_hist"], row.prev("macd_hist")
        if not isnan(hist, prev_hist):
            if hist > 0 and hist >= prev_hist:
                long["macd"] = self.W_MACD
            elif hist < 0 and hist <= prev_hist:
                short["macd"] = self.W_MACD

        vol, vol_sma = row["volume"], row["vol_sma"]
        if not isnan(vol, vol_sma) and vol > vol_sma:
            (long if "structure" in long else short)["volume"] = self.W_VOLUME

        e_close, e_ema = row["e_close"], row["e_ema21"]
        if not isnan(e_close, e_ema):
            if e_close > e_ema:
                long["entry_tf"] = self.W_ENTRY_TF
            elif e_close < e_ema:
                short["entry_tf"] = self.W_ENTRY_TF

        score = self.combine(long, short)
        return SubSignal(self.name, score, {"long": long, "short": short})
