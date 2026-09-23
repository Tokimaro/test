"""Событийный портфельный бэктест (раздел 10 плана).

Использует тот же код, что и live: признаки, SignalEngine, planner, RiskManager, sizing,
position_logic. Отличается только исполнение — оно симулируется по OHLC свечей:

* сигнал на закрытии свечи i → вход по открытию свечи i+1 с проскальзыванием;
* внутри свечи: гэп через стоп → исполнение по open; если в одной свече задеты и стоп,
  и цель — считаем, что сработал стоп (консервативно);
* после TP1 порядок экстремумов внутри свечи берётся из её цвета
  (бычья: O→L→H→C, медвежья: O→H→L→C) — для проверки безубытка;
* комиссии taker на каждое исполнение, funding каждые 8 часов для перпетуалов.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.features import build_features
from app.analysis.regime import Regime
from app.domain import Direction, Instrument
from app.execution.position_logic import (
    ClosePosition,
    CloseReason,
    ManagedPosition,
    MoveStop,
    apply_move,
    on_bar_close,
    on_tp1_filled,
)
from app.risk.manager import RiskEvent, RiskManager
from app.risk.sizing import size_position
from app.strategy.base import MarketContext, Row, columns_of
from app.strategy.ensemble import Signal, SignalEngine, prepare
from app.strategy.planner import PlanRejected, TradePlan, plan_trade
from app.trading_config import MarketSettings, RiskSettings, TradingConfig

FUNDING_PERIOD_MS = 8 * 3_600_000
CORR_WINDOW = 200


@dataclass(frozen=True)
class BacktestSettings:
    initial_equity: float = 10_000.0
    slippage_pct: float = 0.0005
    funding_rate_8h: float = 0.0001  # средний funding; платят лонги при положительном
    taker_fee: float | None = None  # None — из инструмента


@dataclass
class SymbolData:
    symbol: str
    instrument: Instrument
    working: pd.DataFrame
    higher: pd.DataFrame
    entry: pd.DataFrame | None = None


@dataclass
class PreparedSymbol:
    symbol: str
    instrument: Instrument
    index: np.ndarray  # ts открытия свечей рабочего ТФ
    cols: dict[str, np.ndarray]
    signals: list[Signal]


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    direction: str
    strategy: str
    regime: str
    confidence: float
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    qty: float
    pnl: float
    fees: float
    funding: float
    risk_amount: float
    r_multiple: float
    bars_held: int
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
class _Pending:
    signal: Signal
    plan: TradePlan
    risk_pct: float


class Backtester:
    def __init__(self, config: TradingConfig, market: MarketSettings, bt: BacktestSettings) -> None:
        self.config = config
        self.market = market
        self.bt = bt
        self.tf = market.timeframes.working
        self.engine = SignalEngine(config.strategy)

    # ------------------------------------------------------------------ подготовка
    def prepare(self, data: list[SymbolData]) -> list[PreparedSymbol]:
        """Считает признаки и сигналы один раз — дальше их можно прогонять с разными
        настройками риска (walk-forward) без пересчёта."""
        cfg = self.config.strategy
        tfs = self.market.timeframes
        frames: dict[str, pd.DataFrame] = {}
        for d in data:
            feats = build_features(
                d.working, tfs.working, d.higher, tfs.higher, d.entry, tfs.entry, cfg
            )
            frames[d.symbol] = prepare(feats, cfg)

        btc = frames.get("BTCUSDT")
        btc_regime = btc["regime"] if btc is not None else None
        btc_trend = btc["long_trend"] if btc is not None else None

        out = []
        for d in data:
            prepared = frames[d.symbol]
            index = prepared.index.to_numpy(dtype="int64")
            cols = columns_of(prepared)
            signals = []
            for i in range(len(prepared)):
                ctx = None
                if btc_regime is not None:
                    ts = int(index[i])
                    btc_r = btc_regime.get(ts)
                    btc_t = btc_trend.get(ts) if btc_trend is not None else None
                    ctx = MarketContext(
                        btc_regime=Regime(btc_r) if isinstance(btc_r, str) else None,
                        market_trend=int(btc_t) if btc_t is not None else None,
                        is_btc=d.symbol == "BTCUSDT",
                    )
                signals.append(self.engine.evaluate_row(Row(cols, i), int(index[i]), d.symbol, ctx))
            out.append(PreparedSymbol(d.symbol, d.instrument, index, cols, signals))
        return out

    # ------------------------------------------------------------------ прогон
    def run(
        self,
        symbols: list[PreparedSymbol],
        *,
        risk: RiskSettings | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> BacktestResult:
        return _Run(self, symbols, risk or self.config.risk, start_ms, end_ms).execute()


class _Run:
    def __init__(
        self,
        bt: Backtester,
        symbols: list[PreparedSymbol],
        risk: RiskSettings,
        start_ms: int | None,
        end_ms: int | None,
    ) -> None:
        self.bt = bt
        self.symbols = {s.symbol: s for s in symbols}
        self.stops = bt.config.strategy.stops
        self.settings = bt.bt
        self.derivatives = bt.market.category in ("linear", "inverse")
        self.events: list[RiskEvent] = []
        self.risk = RiskManager(risk, on_event=self.events.append)
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.cash = bt.bt.initial_equity
        self.positions: dict[str, ManagedPosition] = {}
        self.leverage: dict[str, float] = {}
        self.pending: dict[str, _Pending] = {}
        self.last_close: dict[str, float] = {}
        self.trades: list[ClosedTrade] = []
        self.curve: list[tuple[int, float]] = []
        self.stats: Counter[str] = Counter()

    def fee(self, sym: PreparedSymbol) -> float:
        if self.settings.taker_fee is not None:
            return self.settings.taker_fee
        return float(sym.instrument.taker_fee)

    def equity(self) -> float:
        unreal = sum(p.unrealized(self.last_close[s]) for s, p in self.positions.items())
        return self.cash + unreal

    def execute(self) -> BacktestResult:
        timeline = np.unique(np.concatenate([s.index for s in self.symbols.values()]))
        if self.start_ms is not None:
            timeline = timeline[timeline >= self.start_ms]
        if self.end_ms is not None:
            timeline = timeline[timeline <= self.end_ms]
        pos_idx = {
            name: dict(zip(s.index.tolist(), range(len(s.index)), strict=True))
            for name, s in self.symbols.items()
        }
        tf_ms = self.bt.tf.ms

        for ts in timeline.tolist():
            active = [(name, pos_idx[name][ts]) for name in self.symbols if ts in pos_idx[name]]
            for name, i in active:
                self.process_bar(self.symbols[name], i, ts)
            eq = self.equity()
            self.curve.append((ts + tf_ms, eq))
            self.risk.on_equity(ts + tf_ms, eq)
            for name, i in active:
                self.consider_signal(self.symbols[name], i, ts)

        # закрываем остатки по последней цене
        if self.curve:
            end_ts = self.curve[-1][0]
            for name in list(self.positions):
                sym = self.symbols[name]
                self.close_all(sym, self.last_close[name], end_ts, CloseReason.END)
            self.curve[-1] = (end_ts, self.cash)
        return BacktestResult(
            trades=self.trades,
            equity_curve=self.curve,
            initial_equity=self.settings.initial_equity,
            bar_ms=tf_ms,
            signal_stats=dict(self.stats),
            risk_events=self.events,
        )

    # ------------------------------------------------------------------ свеча
    def process_bar(self, sym: PreparedSymbol, i: int, ts: int) -> None:
        c = sym.cols
        o, h, lo, cl = (float(c[k][i]) for k in ("open", "high", "low", "close"))
        name = sym.symbol

        pending = self.pending.pop(name, None)
        if pending is not None:
            self.open_position(sym, pending, o, ts)

        pos = self.positions.get(name)
        if pos is not None:
            if ts % FUNDING_PERIOD_MS == 0 and pos.opened_ts < ts:
                self.charge_funding(pos, o)
            self.simulate_intrabar(sym, pos, o, h, lo, cl, ts)

        self.last_close[name] = cl
        pos = self.positions.get(name)
        if pos is not None:
            actions = on_bar_close(
                pos, cl, float(c["chand_long"][i]), float(c["chand_short"][i]), self.stops
            )
            for a in actions:
                if isinstance(a, ClosePosition):
                    self.close_all(
                        sym, self._slip(cl, pos, exit_=True), ts + self.bt.tf.ms, a.reason
                    )
                elif isinstance(a, MoveStop):
                    apply_move(pos, a)
            if name in self.positions:
                self.risk.update_open_risk(name, pos.open_risk())

    def simulate_intrabar(
        self,
        sym: PreparedSymbol,
        pos: ManagedPosition,
        o: float,
        h: float,
        lo: float,
        close: float,
        ts: int,
    ) -> None:
        sign = pos.sign
        name = sym.symbol

        def hit(level: float, favourable: bool) -> bool:
            # для лонга цель — сверху (high), стоп — снизу (low)
            if (sign > 0) == favourable:
                return h >= level
            return lo <= level

        def passed_at_open(level: float, favourable: bool) -> bool:
            return (o - level) * sign >= 0 if favourable else (o - level) * sign <= 0

        # 1. гэп через стоп
        if passed_at_open(pos.stop, favourable=False):
            self.close_all(sym, self._slip(o, pos, exit_=True), ts, pos.stop_kind.close_reason)
            return
        # 2. гэп через цели
        if pos.tp1 is not None and not pos.tp1_done and passed_at_open(pos.tp1, True):
            self.take_tp1(sym, pos, o, ts)
        if name in self.positions and passed_at_open(pos.tp2, True):
            self.close_all(sym, self._slip(o, pos, exit_=True), ts, CloseReason.TP2)
            return
        # 3. стоп задет внутри свечи — считаем, что он был первым
        if hit(pos.stop, favourable=False):
            price = self._slip(pos.stop, pos, exit_=True)
            self.close_all(sym, price, ts, pos.stop_kind.close_reason)
            return
        # 4. TP1 → безубыток; порядок экстремумов по цвету свечи
        if pos.tp1 is not None and not pos.tp1_done and hit(pos.tp1, True):
            self.take_tp1(sym, pos, pos.tp1, ts)
            # обе цели лежат в «сторону прибыли» и достигаются раньше разворота свечи
            if name in self.positions and hit(pos.tp2, True):
                self.close_all(sym, self._slip(pos.tp2, pos, exit_=True), ts, CloseReason.TP2)
                return
            adverse_after = (close < o) if sign > 0 else (close > o)
            if name in self.positions and adverse_after and hit(pos.stop, favourable=False):
                price = self._slip(pos.stop, pos, exit_=True)
                self.close_all(sym, price, ts, pos.stop_kind.close_reason)
                return
        # 5. TP2
        if name in self.positions and hit(pos.tp2, True):
            self.close_all(sym, self._slip(pos.tp2, pos, exit_=True), ts, CloseReason.TP2)

    def _slip(self, price: float, pos: ManagedPosition, *, exit_: bool) -> float:
        # проскальзывание всегда против нас
        s = self.settings.slippage_pct
        side = -pos.sign if exit_ else pos.sign
        return price * (1 + side * s)

    # ------------------------------------------------------------------ сделки
    def open_position(self, sym: PreparedSymbol, p: _Pending, open_price: float, ts: int) -> None:
        plan = p.plan
        sign = plan.direction.sign
        entry = open_price * (1 + sign * self.settings.slippage_pct)
        # цена ушла за стоп или за первую цель ещё до входа — сетап недействителен
        first_target = plan.tp1 if plan.tp1 is not None else plan.tp2
        if (entry - plan.stop) * sign <= 0 or (first_target - entry) * sign <= 0:
            self.stats["skip_gap"] += 1
            return
        eq = self.equity()
        margin_used = sum(
            pos.entry * pos.remaining / self.leverage.get(s, 1.0)
            for s, pos in self.positions.items()
        )
        inst = sym.instrument
        fee = self.fee(sym)
        if self.settings.taker_fee is not None:
            inst = _with_fee(inst, fee)
        sizing = size_position(
            equity=Decimal(str(eq)),
            risk_pct=p.risk_pct,
            direction=plan.direction,
            entry=Decimal(str(entry)),
            stop=Decimal(str(plan.stop)),
            instrument=inst,
            available_margin=Decimal(str(max(0.0, eq - margin_used))),
            max_leverage=Decimal(str(self.risk.settings.max_leverage)),
            atr=Decimal(str(plan.atr)),
            slippage_pct=Decimal(str(self.settings.slippage_pct)),
            derivatives=self.derivatives,
            gap_risk_pct=Decimal(str(self.bt.market.gap_risk_pct)),
        )
        if not sizing.ok:
            self.stats[f"size_{sizing.reject}"] += 1
            return
        qty = float(sizing.qty)
        entry_fee = entry * qty * fee
        self.cash -= entry_fee
        pos = ManagedPosition(
            symbol=sym.symbol,
            direction=plan.direction,
            strategy=plan.strategy,
            entry=entry,
            qty=qty,
            initial_stop=plan.stop,
            stop=plan.stop,
            tp1=plan.tp1,
            tp2=plan.tp2,
            tp1_fraction=plan.tp1_fraction,
            trailing=plan.trailing,
            opened_ts=ts,
            risk_amount=float(sizing.risk_amount),
            confidence=p.signal.confidence,
            regime=p.signal.regime.value,
        )
        pos.fees += entry_fee
        pos.realized_pnl -= entry_fee
        self.positions[sym.symbol] = pos
        self.leverage[sym.symbol] = float(sizing.leverage)
        self.last_close[sym.symbol] = open_price
        self.risk.on_position_opened(sym.symbol, plan.direction, pos.open_risk())
        self.stats["opened"] += 1

    def fill(self, sym: PreparedSymbol, pos: ManagedPosition, price: float, qty: float) -> None:
        fee = price * qty * self.fee(sym)
        gross = (price - pos.entry) * pos.sign * qty
        self.cash += gross - fee
        pos.realized_pnl += gross - fee
        pos.fees += fee
        pos.exit_value += price * qty
        pos.remaining -= qty

    def take_tp1(self, sym: PreparedSymbol, pos: ManagedPosition, level: float, ts: int) -> None:
        assert pos.tp1 is not None
        qty = float(sym.instrument.round_qty(pos.qty * pos.tp1_fraction))
        pos.tp1_done = True
        price = self._slip(level, pos, exit_=True)
        if qty >= pos.remaining:
            self.close_all(sym, price, ts, CloseReason.TP1)
            return
        if qty > 0:  # слишком маленькая позиция для частичного выхода — только безубыток
            self.fill(sym, pos, price, qty)
        move = on_tp1_filled(pos, self.fee(sym))
        if move is not None:
            apply_move(pos, move)
        self.risk.update_open_risk(sym.symbol, pos.open_risk())

    def charge_funding(self, pos: ManagedPosition, price: float) -> None:
        cost = pos.remaining * price * self.settings.funding_rate_8h * pos.sign
        self.cash -= cost
        pos.realized_pnl -= cost
        pos.funding += cost

    def close_all(self, sym: PreparedSymbol, price: float, ts: int, reason: CloseReason) -> None:
        pos = self.positions.pop(sym.symbol)
        self.fill(sym, pos, price, pos.remaining)
        self.leverage.pop(sym.symbol, None)
        self.risk.on_position_closed(sym.symbol)
        self.risk.on_trade_closed(ts, pos.realized_pnl)
        exit_qty = pos.qty
        self.trades.append(
            ClosedTrade(
                symbol=pos.symbol,
                direction=pos.direction.value,
                strategy=pos.strategy,
                regime=pos.regime,
                confidence=pos.confidence,
                entry_ts=pos.opened_ts,
                exit_ts=ts,
                entry=pos.entry,
                exit=pos.exit_value / exit_qty if exit_qty else price,
                qty=pos.qty,
                pnl=pos.realized_pnl,
                fees=pos.fees,
                funding=pos.funding,
                risk_amount=pos.risk_amount,
                r_multiple=pos.realized_pnl / pos.risk_amount if pos.risk_amount > 0 else 0.0,
                bars_held=pos.bars_held,
                close_reason=reason.value,
            )
        )

    # ------------------------------------------------------------------ сигналы
    def consider_signal(self, sym: PreparedSymbol, i: int, ts: int) -> None:
        signal = sym.signals[i]
        if signal.direction is None:
            return
        name = sym.symbol
        if name in self.positions or name in self.pending:
            return
        if signal.direction is Direction.SHORT and not self.bt.market.allow_short:
            self.stats["reject_short_not_allowed"] += 1
            return
        self.stats["signals"] += 1
        corr = self.correlations(sym, i) if self.risk.state.open else None
        decision = self.risk.check_new_trade(ts, name, signal.direction, signal.confidence, corr)
        if not decision.allowed:
            self.stats[f"reject_{decision.reason}"] += 1
            return
        plan = plan_trade(signal, Row(sym.cols, i), self.stops)
        if isinstance(plan, PlanRejected):
            self.stats[f"plan_{plan.reason.split(':')[0]}"] += 1
            return
        self.pending[name] = _Pending(signal, plan, decision.risk_pct)

    def correlations(self, sym: PreparedSymbol, i: int) -> dict[str, float]:
        out: dict[str, float] = {}
        ts = int(sym.index[i])
        mine = _log_returns(sym.cols["close"][max(0, i - CORR_WINDOW) : i + 1])
        for other_name in self.risk.state.open:
            other = self.symbols[other_name]
            j = int(np.searchsorted(other.index, ts, side="right")) - 1
            if j < 1:
                continue
            theirs = _log_returns(other.cols["close"][max(0, j - CORR_WINDOW) : j + 1])
            n = min(len(mine), len(theirs))
            if n >= 20:
                out[other_name] = float(np.corrcoef(mine[-n:], theirs[-n:])[0, 1])
        return out


def _log_returns(closes: np.ndarray) -> np.ndarray:
    arr = closes.astype("float64")
    return np.diff(np.log(arr))


def _with_fee(inst: Instrument, fee: float) -> Instrument:
    return replace(inst, taker_fee=Decimal(str(fee)))
