"""Дневной тренд «лонг или кэш» — стратегия бота.

Формулы совпадают с исследовательским кодом (app/research/strategies.py::tsmom с long_only,
max_gross=1): это проверяет tests/test_trend.py, чтобы бот торговал ровно то, что проверялось
(docs/research-strategies.md, docs/volatile-pairs.md).

По дневным ценам закрытия (строка t — только данные до t включительно):

* сигнал монеты = среднее sign(close_t / close_{t−L} − 1) по L из lookbacks, отрицательная
  часть обрезается (падающая монета → 0, то есть кэш);
* монета участвует, когда у неё есть max(lookbacks)+1 дней истории;
* доля = сигнал / √(число участвующих монет) × целевая волатильность / волатильность монеты
  (годовая, по vol_lookback дням; множитель не больше 5);
* сумма долей ≤ 100% капитала (спот без плеча); доля одной монеты ≤ max_weight;
* с btc_filter все доли обнуляются, пока BTC ниже своей btc_ma_days-дневной средней.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.trading_config import RiskSettings, StrategySettings

STRATEGY_NAME = "trend"
DAYS_PER_YEAR = 365.0
MAX_VOL_SCALE = 5.0
BTC = "BTCUSDT"


@dataclass(frozen=True, slots=True)
class Signal:
    """Целевая доля монеты на дату: для журнала, панели и уведомлений."""

    ts: int  # время открытия дневной свечи
    symbol: str
    weight: float  # целевая доля капитала, 0..1
    score: float  # сигнал тренда 0..1 (доля горизонтов с ростом)
    components: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> str | None:
        return "long" if self.weight > 0 else None


def target_weights(
    closes: pd.DataFrame, cfg: StrategySettings, risk: RiskSettings
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Целевые доли на каждую дату. closes — цены закрытия (индекс — ts, колонки — монеты;
    колонка BTCUSDT нужна при btc_filter). Возвращает (доли, сигналы)."""
    lookbacks = cfg.lookbacks
    min_history = max(lookbacks) + 1
    avail = closes.notna() & (closes.notna().cumsum() >= min_history)
    sig = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for lb in lookbacks:
        sig = sig + np.sign(closes / closes.shift(lb) - 1)
    sig = (sig / len(lookbacks)).where(avail, 0.0).fillna(0.0).clip(lower=0)
    n = avail.sum(axis=1).clip(lower=1)
    raw = sig.div(np.sqrt(n), axis=0)

    vol = closes.pct_change().rolling(cfg.vol_lookback, min_periods=cfg.vol_lookback).std()
    vol = vol * np.sqrt(DAYS_PER_YEAR)
    w = raw * (risk.target_vol_pct / 100 / vol).clip(upper=MAX_VOL_SCALE)
    gross = w.abs().sum(axis=1)
    w = w.mul((1.0 / gross).clip(upper=1).fillna(1), axis=0).fillna(0.0)
    if risk.max_weight_pct < 100:
        w = w.clip(upper=risk.max_weight_pct / 100)  # излишек остаётся в кэше
    if cfg.btc_filter:
        if BTC not in closes:
            raise ValueError("btc_filter требует цен BTCUSDT")
        btc = closes[BTC]
        on = btc > btc.rolling(cfg.btc_ma_days, min_periods=cfg.btc_ma_days).mean()
        w = w.mul(on.astype(float), axis=0)
    return w, sig


def latest_signals(
    closes: pd.DataFrame, symbols: list[str], cfg: StrategySettings, risk: RiskSettings
) -> list[Signal]:
    """Сигналы по последней закрытой дневной свече для торгуемых монет."""
    weights, sig = target_weights(closes, cfg, risk)
    ts = int(closes.index[-1])
    out = []
    for s in symbols:
        c = closes[s]
        comp: dict[str, Any] = {f"ret_{lb}d": _ret(c, lb) for lb in cfg.lookbacks}
        vol = c.pct_change().tail(cfg.vol_lookback).std() * np.sqrt(DAYS_PER_YEAR)
        comp["vol_pct"] = round(float(vol) * 100, 1) if vol == vol else None
        comp["history_days"] = int(c.notna().sum())
        if cfg.btc_filter and BTC in closes:
            btc = closes[BTC]
            comp["btc_above_ma"] = bool(
                btc.iloc[-1] > btc.tail(cfg.btc_ma_days).mean()
                and btc.notna().sum() >= cfg.btc_ma_days
            )
        out.append(
            Signal(
                ts,
                s,
                round(float(weights[s].iloc[-1]), 6),
                round(float(sig[s].iloc[-1]), 4),
                comp,
            )
        )
    return out


def _ret(c: pd.Series, lb: int) -> float | None:
    if c.notna().sum() <= lb:
        return None
    r = float(c.iloc[-1] / c.iloc[-1 - lb] - 1)
    return round(r * 100, 2)
