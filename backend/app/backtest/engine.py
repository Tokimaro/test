"""Бэктест стратегии дневного тренда на истории дневных свечей.

Использует тот же код расчёта долей, что и live (app.strategy.trend.target_weights), и те же
правила ребалансировки, что и движок (app.core.engine):

* доли считаются по закрытию дня t; в день ребалансировки (день после t — rebalance_weekday)
  портфель перестраивается по цене закрытия t с проскальзыванием против нас;
* сначала продажи, потом покупки на свободный кэш; изменения меньше min_trade_pct% капитала
  пропускаются, выход из монеты (доля 0) — всегда целиком;
* комиссия — taker инструмента (спот Bybit 0.1%) на каждое исполнение;
* владение монетой от первой покупки до полной продажи — одна «сделка» в отчёте.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.domain import Instrument
from app.risk.manager import RiskEvent, RiskManager
from app.strategy import trend
from app.trading_config import MarketSettings, RiskSettings, TradingConfig

DAY_MS = 86_400_000


@dataclass(frozen=True)
class BacktestSettings:
    initial_equity: float = 10_000.0
    slippage_pct: float = 0.0005
    fee: float | None = None  # None — taker_fee инструмента


@dataclass
class SymbolData:
    symbol: str
    instrument: Instrument
    daily: pd.DataFrame  # дневные свечи: индекс — ts открытия (мс), колонка close


@dataclass
class Prepared:
    closes: pd.DataFrame
    instruments: dict[str, Instrument]


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    direction: str
    strategy: str
    regime: str
    confidence: float  # целевая доля при входе, %
    entry_ts: int
    exit_ts: int
    entry: float  # средняя цена покупок
    exit: float
    stop: float
    qty: float  # наибольший объём за время владения
    pnl: float
    fees: float
    funding: float
    risk_amount: float  # вложено за всё время владения, USDT
    r_multiple: float  # доходность владения: pnl / вложено
    bars_held: int  # дней
    close_reason: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity_curve: list[tuple[int, float]]
    initial_equity: float
    bar_ms: int
    signal_stats: dict[str, int] = field(default_factory=dict)
    risk_events: list[RiskEvent] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_equity


@dataclass
class _Holding:
    qty: float = 0.0
    max_qty: float = 0.0
    avg: float = 0.0
    invested: float = 0.0
    realized: float = 0.0
    fees: float = 0.0
    opened: int = 0
    weight: float = 0.0


class Backtester:
    def __init__(self, config: TradingConfig, market: MarketSettings, bt: BacktestSettings) -> None:
        self.config = config
        self.market = market
        self.bt = bt

    def prepare(self, data: list[SymbolData]) -> Prepared:
        closes = pd.DataFrame({d.symbol: d.daily["close"] for d in data}).sort_index()
        return Prepared(closes.astype("float64"), {d.symbol: d.instrument for d in data})

    def run(
        self,
        prepared: Prepared,
        *,
        risk: RiskSettings | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> BacktestResult:
        return _Run(self, prepared, risk or self.config.risk, start_ms, end_ms).execute()


class _Run:
    def __init__(
        self,
        bt: Backtester,
        prepared: Prepared,
        risk: RiskSettings,
        start_ms: int | None,
        end_ms: int | None,
    ) -> None:
        self.bt = bt
        self.cfg = bt.config.strategy
        self.risk_settings = risk
        self.p = prepared
        self.events: list[RiskEvent] = []
        self.risk = RiskManager(risk, on_event=self.events.append)
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.cash = bt.bt.initial_equity
        self.holdings: dict[str, _Holding] = {}
        self.trades: list[ClosedTrade] = []
        self.curve: list[tuple[int, float]] = []
        self.stats: Counter[str] = Counter()

    def fee(self, symbol: str) -> float:
        if self.bt.bt.fee is not None:
            return self.bt.bt.fee
        return float(self.p.instruments[symbol].taker_fee)

    def equity(self, prices: pd.Series) -> float:
        return self.cash + sum(h.qty * float(prices[s]) for s, h in self.holdings.items())

    def execute(self) -> BacktestResult:
        closes = self.p.closes
        symbols = [s for s in self.bt.market.symbols if s in closes]
        weights, _ = trend.target_weights(closes, self.cfg, self.risk_settings)
        last_prices = closes.ffill()
        first = True
        halted = False
        for pos, ts in enumerate(closes.index.to_numpy(dtype="int64").tolist()):
            if self.start_ms is not None and ts < self.start_ms:
                continue
            if self.end_ms is not None and ts > self.end_ms:
                break
            prices = last_prices.iloc[pos]
            if not halted and self.risk.on_equity(ts + DAY_MS, self.equity(prices)):
                halted = True  # остановка по просадке: всё в кэш до конца прогона
                self.stats["drawdown_stop"] += 1
                for s in list(self.holdings):
                    self.sell(s, None, float(prices[s]), ts, "drawdown_stop")
            if not halted and (first or _weekday(ts + DAY_MS) == self.cfg.rebalance_weekday):
                self.rebalance(symbols, weights.iloc[pos], prices, ts)
                first = False
            self.curve.append((ts + DAY_MS, self.equity(prices)))
        if self.curve:
            end_ts = self.curve[-1][0]
            last = last_prices.iloc[-1]
            for s in list(self.holdings):
                self.close(s, float(last[s]), end_ts, "end")
            self.curve[-1] = (end_ts, self.cash)
        return BacktestResult(
            trades=self.trades,
            equity_curve=self.curve,
            initial_equity=self.bt.bt.initial_equity,
            bar_ms=DAY_MS,
            signal_stats=dict(self.stats),
            risk_events=self.events,
        )

    def rebalance(self, symbols: list[str], w: pd.Series, prices: pd.Series, ts: int) -> None:
        self.stats["rebalances"] += 1
        capital = self.equity(prices)
        min_trade = self.cfg.min_trade_pct / 100 * capital
        buys: list[tuple[str, float]] = []
        for s in symbols:
            price = float(prices[s])
            if price != price:  # монеты ещё нет на рынке
                continue
            h = self.holdings.get(s)
            held = h.qty if h else 0.0
            target = float(w.get(s, 0.0))
            diff = target * capital - held * price
            if target == 0 and held > 0:
                self.sell(s, None, price, ts, "signal")
            elif diff <= -min_trade and held > 0:
                self.sell(s, -diff / price, price, ts, "signal")
            elif diff >= min_trade:
                buys.append((s, diff / price))
        slip = 1 + self.bt.bt.slippage_pct
        need = sum(q * float(prices[s]) * slip * (1 + self.fee(s)) for s, q in buys)
        scale = min(1.0, self.cash / need) if need > 0 else 0.0
        for s, q in buys:
            self.buy(s, q * scale, float(prices[s]), ts, float(w.get(s, 0.0)))

    def buy(self, s: str, qty: float, price: float, ts: int, weight: float) -> None:
        inst = self.p.instruments[s]
        q = float(inst.round_qty(qty))
        if q <= 0 or q < float(inst.min_qty):
            return
        px = price * (1 + self.bt.bt.slippage_pct)
        fee = px * q * self.fee(s)
        self.cash -= px * q + fee
        h = self.holdings.get(s)
        if h is None:
            h = self.holdings[s] = _Holding(opened=ts + DAY_MS, weight=weight)
            self.stats["opened"] += 1
        h.avg = (h.avg * h.qty + px * q) / (h.qty + q)
        h.qty += q
        h.max_qty = max(h.max_qty, h.qty)
        h.invested += px * q
        h.fees += fee
        h.realized -= fee
        self.stats["orders"] += 1

    def sell(self, s: str, qty: float | None, price: float, ts: int, reason: str) -> None:
        h = self.holdings[s]
        inst = self.p.instruments[s]
        q = h.qty if qty is None else min(h.qty, float(inst.round_qty(qty)))
        if q <= 0:
            return
        px = price * (1 - self.bt.bt.slippage_pct)
        fee = px * q * self.fee(s)
        self.cash += px * q - fee
        h.realized += (px - h.avg) * q - fee
        h.fees += fee
        h.qty -= q
        self.stats["orders"] += 1
        if qty is None or h.qty < float(inst.min_qty):
            self.close(s, px, ts + DAY_MS, reason)

    def close(self, s: str, px: float, ts: int, reason: str) -> None:
        """Закрывает владение; остаток (конец данных или «пыль») продаётся по px."""
        h = self.holdings.pop(s)
        if h.qty > 0:
            fee = px * h.qty * self.fee(s)
            self.cash += px * h.qty - fee
            h.realized += (px - h.avg) * h.qty - fee
            h.fees += fee
            h.qty = 0.0
        self.trades.append(
            ClosedTrade(
                symbol=s,
                direction="long",
                strategy=trend.STRATEGY_NAME,
                regime="",
                confidence=round(h.weight * 100, 2),
                entry_ts=h.opened,
                exit_ts=ts,
                entry=h.avg,
                exit=px,
                stop=0.0,
                qty=h.max_qty,
                pnl=h.realized,
                fees=h.fees,
                funding=0.0,
                risk_amount=h.invested,
                r_multiple=h.realized / h.invested if h.invested > 0 else 0.0,
                bars_held=int((ts - h.opened) // DAY_MS),
                close_reason=reason,
            )
        )


def _weekday(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).weekday()
