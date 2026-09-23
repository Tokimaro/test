"""Торговый движок для paper/live (раздел 3 плана).

Поток: закрылась свеча рабочего ТФ → признаки → сигнал → риск-проверка → план → объём →
ордер с SL/TP на бирже. Открытые позиции ведутся на каждой свече (трейлинг, тайм-стоп), а
сверка с биржей (reconcile) раз в N секунд обнаруживает закрытия по SL/TP, исполнение TP1,
позиции без стопа и «чужие» позиции. Биржа — источник истины.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from app.analysis.features import build_features
from app.analysis.indicators import candles_to_frame
from app.analysis.regime import Regime
from app.brokers.base import BrokerAdapter, BrokerError, CandleClosed
from app.brokers.paper import PaperBroker
from app.core.events import EventBus
from app.db.candles import ms_to_dt
from app.db.models import TradeRow
from app.db.repo import TradeRepo
from app.domain import Candle, Direction, Position, Timeframe
from app.execution.executor import OrderExecutor, OrderUncertain
from app.execution.position_logic import (
    ClosePosition,
    CloseReason,
    ManagedPosition,
    MoveStop,
    StopKind,
    apply_move,
    on_bar_close,
    on_tp1_filled,
)
from app.market.store import CandleStore
from app.risk.circuit import CircuitBreaker
from app.risk.correlation import correlation_matrix, correlations_for
from app.risk.manager import RiskEvent, RiskManager
from app.risk.sizing import size_position
from app.strategy.base import MarketContext, Row, columns_of
from app.strategy.ensemble import Signal, SignalEngine, prepare
from app.strategy.planner import PlanRejected, plan_trade
from app.trading_config import MarketSettings, TradingConfig

log = structlog.get_logger()

HISTORY = {"working": 2500, "higher": 600, "entry": 400}
FINALIZE_ATTEMPTS = 5  # сколько сверок ждать данных о закрытии от биржи
CONFIRM_ATTEMPTS = 10  # сколько сверок ждать позицию после входа с неизвестным исходом


def wall_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Tracked:
    trade_id: int
    market: str
    pos: ManagedPosition
    tp1_link_id: str | None = None
    finalize_attempts: int = 0
    pending_reason: CloseReason | None = None
    # False — ордер входа отправлен, но исход неизвестен (сеть/рестарт): позиция либо
    # появится на бирже и будет «подхвачена» сверкой, либо сделка будет отменена
    confirmed: bool = True
    confirm_attempts: int = 0


class TradingEngine:
    def __init__(
        self,
        *,
        config: TradingConfig,
        brokers: dict[str, BrokerAdapter],
        store: CandleStore,
        repo: TradeRepo,
        bus: EventBus,
        instrument_ids: dict[str, int],
        ensure_fresh: Callable[[str, Timeframe], Awaitable[None]] | None = None,
        clock: Callable[[], int] = wall_ms,
    ) -> None:
        self.config = config
        self.brokers = brokers
        self.store = store
        self.repo = repo
        self.bus = bus
        self.instrument_ids = instrument_ids
        self.ensure_fresh = ensure_fresh
        self.clock = clock
        self.signal_engine = SignalEngine(config.strategy)
        self.risk = RiskManager(config.risk, on_event=self._on_risk_event)
        self.executors = {m: OrderExecutor(b, repo) for m, b in brokers.items()}
        self.markets: dict[str, MarketSettings] = {
            name: m for name, m in config.markets.items() if m.enabled and name in brokers
        }
        self.symbol_market = {s: name for name, m in self.markets.items() for s in m.symbols}
        self.tracked: dict[str, Tracked] = {}
        self.paused = False
        self.btc_regime: Regime | None = None
        self.last_signal: dict[str, Signal] = {}
        self._lock = asyncio.Lock()
        self._unmanaged_alerted: set[str] = set()
        self._bg: set[asyncio.Task[None]] = set()
        self.breaker = CircuitBreaker()

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        state = await self.repo.get_state("risk")
        if state:
            self.risk.state = RiskManager.state_from_dict(state)
        for row in await self.repo.open_trades():
            symbol = await self._symbol_of(row)
            if symbol is None or symbol not in self.symbol_market:
                log.warning("engine.orphan_trade", trade_id=row.id)
                continue
            self.tracked[symbol] = Tracked(
                trade_id=row.id,
                market=self.symbol_market[symbol],
                pos=_position_from_row(row, symbol),
                tp1_link_id=(row.extra or {}).get("tp1_link_id"),
                # вход не подтвердился до рестарта — сверка подхватит позицию или отменит
                confirmed=row.status == "open" and row.entry_price is not None,
            )
        await self.reconcile()
        self.bus.publish("bot_status", **self.status())

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "halted": self.risk.state.halted,
            "halt_reason": self.risk.state.halt_reason,
            "open_positions": sum(1 for t in self.tracked.values() if t.confirmed),
            "risk_pct": self.risk.current_risk_pct(),
            "drawdown_pct": round(self.risk.drawdown_pct(), 2),
            "circuit_breaker": self.breaker.tripped,
        }

    async def _symbol_of(self, row: TradeRow) -> str | None:
        for sym, iid in self.instrument_ids.items():
            if iid == row.instrument_id:
                return sym
        return None

    def _on_risk_event(self, event: RiskEvent) -> None:
        task = asyncio.get_running_loop().create_task(self.repo.save_risk_event(event))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        self.bus.publish("alert", level="warning", kind=event.type, details=event.details)

    def _api_error(self, kind: str, **info: Any) -> None:
        """Учёт сбоев API: серия ошибок ставит торговлю на паузу (circuit breaker)."""
        now = self.clock()
        if self.breaker.record(now):
            self.paused = True
            self._on_risk_event(RiskEvent(now, "circuit_breaker", {"last_error": kind, **info}))
            self.bus.publish("bot_status", **self.status())

    # ------------------------------------------------------------------ свечи
    async def on_candle(self, event: CandleClosed) -> None:
        market_name = self.symbol_market.get(event.symbol)
        if market_name is None:
            return
        market = self.markets[market_name]
        broker = self.brokers[market_name]
        # paper-брокеру отдаём только самый мелкий ТФ: крупные свечи повторно «проигрывали» бы
        # экстремумы, уже обработанные мелкими
        if isinstance(broker, PaperBroker):
            if event.timeframe is market.timeframes.entry:
                await broker.on_candle(event.symbol, event.candle, event.timeframe.ms)
            elif event.timeframe is market.timeframes.working:
                # вход по закрытию рабочей свечи — по её цене, даже если 15m ещё не пришла
                broker.mark_price(
                    event.symbol, event.candle.close, event.candle.ts + event.timeframe.ms
                )
        if event.timeframe is not market.timeframes.working:
            return
        async with self._lock:
            await self._on_working_candle(event.symbol, market_name, market)

    async def on_backfill(self, symbol: str, tf: Timeframe, candles: list[Candle]) -> None:
        """Свечи, докачанные после обрыва потока: paper-брокер проверяет по ним стопы/цели,
        иначе пропущенные экстремумы не сработали бы (в отличие от настоящей биржи)."""
        market_name = self.symbol_market.get(symbol)
        if market_name is None:
            return
        broker = self.brokers[market_name]
        if isinstance(broker, PaperBroker) and tf is self.markets[market_name].timeframes.entry:
            for c in candles:
                await broker.on_candle(symbol, c, tf.ms)

    async def _on_working_candle(
        self, symbol: str, market_name: str, market: MarketSettings
    ) -> None:
        tfs = market.timeframes
        if self.ensure_fresh is not None:
            await self.ensure_fresh(symbol, tfs.higher)
            await self.ensure_fresh(symbol, tfs.entry)
        feats = await self._features(symbol, market)
        if feats is None or feats.empty:
            return
        broker = self.brokers[market_name]
        ctx = await self._context(symbol, broker)
        signal = self.signal_engine.evaluate_last(feats, symbol, ctx)
        if symbol == "BTCUSDT":
            self.btc_regime = signal.regime
        self.last_signal[symbol] = signal
        row = Row(columns_of(feats), len(feats) - 1)

        acted, reason = False, signal.components.get("reject")
        if symbol in self.tracked:
            await self._manage(symbol, row)
            reason = "position_open" if signal.direction else reason
        elif signal.direction is not None:
            acted, reason = await self._try_enter(symbol, market_name, market, signal, row, feats)

        iid = self.instrument_ids.get(symbol)
        signal_id = None
        if iid is not None:
            signal_id = await self.repo.save_signal(iid, signal, acted, reason)
        if acted and symbol in self.tracked:
            await self.repo.update_trade(self.tracked[symbol].trade_id, signal_id=signal_id)
        self.bus.publish(
            "signal",
            symbol=symbol,
            direction=signal.direction.value if signal.direction else None,
            confidence=signal.confidence,
            regime=signal.regime.value,
            strategy=signal.strategy,
            acted=acted,
            reason=reason,
        )

    async def _features(self, symbol: str, market: MarketSettings) -> pd.DataFrame | None:
        tfs = market.timeframes
        working = await self.store.get_candles(symbol, tfs.working, limit=HISTORY["working"])
        higher = await self.store.get_candles(symbol, tfs.higher, limit=HISTORY["higher"])
        entry = await self.store.get_candles(symbol, tfs.entry, limit=HISTORY["entry"])
        if len(working) < 250 or len(higher) < 50:
            log.info("engine.not_enough_history", symbol=symbol, working=len(working))
            return None
        feats = build_features(
            candles_to_frame(working),
            tfs.working,
            candles_to_frame(higher),
            tfs.higher,
            candles_to_frame(entry) if entry else None,
            tfs.entry,
            self.config.strategy,
        )
        return prepare(feats, self.config.strategy)

    async def _context(self, symbol: str, broker: BrokerAdapter) -> MarketContext:
        funding = None
        try:
            funding = await broker.get_funding_rate(symbol)
        except BrokerError as exc:
            log.warning("engine.funding_unavailable", symbol=symbol, error=str(exc))
        return MarketContext(
            funding_rate=funding, btc_regime=self.btc_regime, is_btc=symbol == "BTCUSDT"
        )

    # ------------------------------------------------------------------ вход
    async def _try_enter(
        self,
        symbol: str,
        market_name: str,
        market: MarketSettings,
        signal: Signal,
        row: Row,
        feats: pd.DataFrame,
    ) -> tuple[bool, str | None]:
        assert signal.direction is not None
        now = self.clock()
        if self.paused:
            return False, "paused"
        broker = self.brokers[market_name]
        if not broker.is_market_open(symbol, now):
            return False, "market_closed"
        corr = await self._correlations(symbol, market) if self.risk.state.open else None
        decision = self.risk.check_new_trade(now, symbol, signal.direction, signal.confidence, corr)
        if not decision.allowed:
            return False, decision.reason
        plan = plan_trade(signal, row, self.config.strategy.stops)
        if isinstance(plan, PlanRejected):
            return False, plan.reason

        inst = await broker.get_instrument(symbol)
        # позиция на бирже, о которой движок не знает (ручная, из прошлого запуска) —
        # новый вход добавился бы к ней и переписал её стоп
        if any(p.symbol == symbol for p in await broker.get_positions()):
            return False, "exchange_position_exists"
        balance = await broker.get_balance()
        stop = inst.round_price(plan.stop)
        tp2 = inst.round_price(plan.tp2)
        tp1 = inst.round_price(plan.tp1) if plan.tp1 is not None else None
        derivatives = market.category in ("linear", "inverse")
        sizing = size_position(
            equity=balance.equity,
            risk_pct=decision.risk_pct,
            direction=plan.direction,
            entry=Decimal(str(plan.entry)),
            stop=stop,
            instrument=inst,
            available_margin=balance.available,
            max_leverage=Decimal(str(self.risk.settings.max_leverage)),
            atr=Decimal(str(plan.atr)),
            derivatives=derivatives,
        )
        if not sizing.ok:
            return False, f"size_{sizing.reject}"

        pos = ManagedPosition(
            symbol=symbol,
            direction=plan.direction,
            strategy=plan.strategy,
            entry=plan.entry,
            qty=float(sizing.qty),
            initial_stop=float(stop),
            stop=float(stop),
            tp1=float(tp1) if tp1 is not None else None,
            tp2=float(tp2),
            tp1_fraction=plan.tp1_fraction,
            trailing=plan.trailing,
            opened_ts=now,
            risk_amount=float(sizing.risk_amount),
            confidence=signal.confidence,
            regime=signal.regime.value,
        )
        extra = {
            "regime": signal.regime.value,
            "plan_entry": plan.entry,
            "leverage": str(sizing.leverage),
            "tp1_fraction": pos.tp1_fraction,
            "trailing": pos.trailing,
        }
        trade_id = await self.repo.create_trade(
            instrument_id=self.instrument_ids[symbol],
            strategy=plan.strategy,
            direction=plan.direction.value,
            status="pending",
            qty=sizing.qty,
            remaining_qty=sizing.qty,
            initial_stop=stop,
            stop_loss=stop,
            tp1=tp1,
            tp2=tp2,
            risk_amount=sizing.risk_amount,
            confidence=signal.confidence,
            opened_at=ms_to_dt(now),
            extra=extra,
        )
        try:
            fill = await self.executors[market_name].open_position(
                trade_id=trade_id,
                instrument=inst,
                direction=plan.direction,
                qty=sizing.qty,
                leverage=sizing.leverage,
                stop=stop,
                take_profit=tp2,
                tp1=tp1,
                tp1_fraction=plan.tp1_fraction,
                derivatives=derivatives,
            )
        except OrderUncertain as exc:
            # ордер мог исполниться: держим сделку неподтверждённой, сверка решит её судьбу,
            # а до тех пор новые входы по символу заблокированы
            log.error("engine.entry_uncertain", symbol=symbol, error=str(exc))
            self._api_error("entry_uncertain", symbol=symbol)
            self.bus.publish(
                "alert", level="error", kind="entry_uncertain", symbol=symbol, error=str(exc)
            )
            self.tracked[symbol] = Tracked(trade_id, market_name, pos, confirmed=False)
            return False, "order_uncertain"
        except BrokerError as exc:
            log.error("engine.entry_rejected", symbol=symbol, error=str(exc))
            await self.repo.update_trade(trade_id, status="cancelled", close_reason="rejected")
            return False, f"broker_rejected:{exc.code}"

        if fill.tp1_link_id is None:
            # без TP1 остаток сразу ведётся трейлингом
            pos.tp1, pos.tp1_fraction = None, 0.0
        if fill.tp1_error:
            self.bus.publish(
                "alert", level="error", kind="tp1_failed", symbol=symbol, error=fill.tp1_error
            )
        self.tracked[symbol] = Tracked(trade_id, market_name, pos, fill.tp1_link_id)
        await self._confirm(symbol, fill.entry_price, fill.qty, fill.tp1_link_id)
        return True, None

    async def _confirm(
        self, symbol: str, entry: Decimal, qty: Decimal, tp1_link_id: str | None
    ) -> None:
        """Позиция подтверждена биржей: фиксируем фактический вход и учитываем риск."""
        tr = self.tracked[symbol]
        pos = tr.pos
        pos.entry, pos.qty, pos.remaining = float(entry), float(qty), float(qty)
        tr.confirmed = True
        trade = await self.repo.get_trade(tr.trade_id)
        extra = dict(trade.extra or {}) if trade is not None else {}
        extra.update(tp1_link_id=tp1_link_id, tp1_fraction=pos.tp1_fraction, trailing=pos.trailing)
        await self.repo.update_trade(
            tr.trade_id,
            status="open",
            entry_price=entry,
            qty=qty,
            remaining_qty=qty,
            tp1=Decimal(str(pos.tp1)) if pos.tp1 is not None else None,
            extra=extra,
        )
        self.risk.on_position_opened(symbol, pos.direction, pos.open_risk())
        await self._save_risk_state()
        self.bus.publish(
            "trade_opened",
            trade_id=tr.trade_id,
            symbol=symbol,
            direction=pos.direction.value,
            entry=pos.entry,
            qty=pos.qty,
            stop=pos.stop,
            tp1=pos.tp1,
            tp2=pos.tp2,
            confidence=pos.confidence,
            strategy=pos.strategy,
        )
        log.info("engine.trade_opened", symbol=symbol, trade_id=tr.trade_id)

    async def _correlations(self, symbol: str, market: MarketSettings) -> dict[str, float]:
        tf = market.timeframes.working
        closes = {}
        for s in {symbol, *self.risk.state.open}:
            candles = await self.store.get_candles(s, tf, limit=201)
            closes[s] = pd.Series([c.close for c in candles], index=[c.ts for c in candles])
        return correlations_for(correlation_matrix(closes), symbol)

    # ------------------------------------------------------------------ ведение позиции
    async def _manage(self, symbol: str, row: Row) -> None:
        tr = self.tracked[symbol]
        if not tr.confirmed or tr.finalize_attempts > 0:
            return  # вход не подтверждён или позиция уже закрыта — решит сверка
        pos = tr.pos
        actions = on_bar_close(
            pos, row["close"], row["chand_long"], row["chand_short"], self.config.strategy.stops
        )
        await self.repo.update_trade(tr.trade_id, bars_held=pos.bars_held)
        broker = self.brokers[tr.market]
        for action in actions:
            if isinstance(action, ClosePosition):
                await self.close_trade(symbol, action.reason)
            elif isinstance(action, MoveStop):
                await self._move_stop(symbol, action, broker)
        if symbol in self.tracked and self.tracked[symbol].pending_reason is not None:
            await self._reconcile_market(tr.market)

    async def _move_stop(self, symbol: str, move: MoveStop, broker: BrokerAdapter) -> None:
        tr = self.tracked[symbol]
        inst = await broker.get_instrument(symbol)
        price = inst.round_price(move.price)
        try:
            await broker.amend_stops(symbol, stop_loss=price)
        except BrokerError as exc:
            log.error("engine.move_stop_failed", symbol=symbol, error=str(exc))
            self._api_error("move_stop_failed", symbol=symbol)
            return
        apply_move(tr.pos, MoveStop(float(price), move.kind))
        await self.repo.update_trade(tr.trade_id, stop_loss=price)
        self.risk.update_open_risk(symbol, tr.pos.open_risk())
        self.bus.publish(
            "trade_updated",
            trade_id=tr.trade_id,
            symbol=symbol,
            stop=float(price),
            kind=move.kind.value,
        )

    async def close_trade(self, symbol: str, reason: CloseReason) -> bool:
        """Закрытие по решению бота (тайм-стоп, вручную, kill switch).

        Итог (PnL) фиксирует сверка. При ошибке причина сбрасывается, чтобы позиция
        продолжала вестись и закрытие повторилось (тайм-стоп — на следующей свече)."""
        tr = self.tracked.get(symbol)
        if tr is None or not tr.confirmed:
            return False
        tr.pending_reason = reason
        try:
            await self.executors[tr.market].close_position(tr.trade_id, symbol)
        except BrokerError as exc:
            tr.pending_reason = None
            log.error("engine.close_failed", symbol=symbol, error=str(exc))
            self._api_error("close_failed", symbol=symbol)
            self.bus.publish(
                "alert", level="error", kind="close_failed", symbol=symbol, error=str(exc)
            )
            return False
        return True

    # ------------------------------------------------------------------ сверка
    async def reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_all()

    async def _reconcile_all(self) -> None:
        for market_name in self.markets:
            await self._reconcile_market(market_name)
        await self._snapshot_equity()
        await self._save_risk_state()

    async def _reconcile_market(self, market_name: str) -> None:
        broker = self.brokers[market_name]
        try:
            positions = {p.symbol: p for p in await broker.get_positions()}
        except BrokerError as exc:
            log.error("engine.reconcile_failed", market=market_name, error=str(exc))
            self._api_error("reconcile_failed", market=market_name)
            return
        for symbol in list(self.tracked):
            tr = self.tracked.get(symbol)
            if tr is None or tr.market != market_name:
                continue
            ex = positions.get(symbol)
            if not tr.confirmed:
                await self._confirm_or_cancel(symbol, ex)
                continue
            if ex is None or ex.direction is not tr.pos.direction:
                await self._finalize(symbol, broker)
                continue
            pos = tr.pos
            ex_qty = float(ex.qty)
            if not pos.tp1_done and ex_qty < pos.remaining * 0.999:
                # частичное исполнение (TP1) — стоп в безубыток
                await self._on_tp1(symbol, ex_qty, broker)
            if ex.stop_loss is None:
                # позиция без стопа недопустима — восстанавливаем немедленно
                log.error("engine.missing_stop", symbol=symbol)
                self.bus.publish("alert", level="error", kind="missing_stop", symbol=symbol)
                try:
                    inst = await broker.get_instrument(symbol)
                    await broker.amend_stops(symbol, stop_loss=inst.round_price(pos.stop))
                except BrokerError as exc:
                    log.error("engine.restore_stop_failed", symbol=symbol, error=str(exc))
                    self._api_error("restore_stop_failed", symbol=symbol)
                    # закрываем; финализация — на следующей сверке (без вложенного вызова)
                    await self.close_trade(symbol, CloseReason.KILL)
        for symbol in positions:
            if symbol not in self.tracked and symbol not in self._unmanaged_alerted:
                self._unmanaged_alerted.add(symbol)
                self.bus.publish("alert", level="warning", kind="unmanaged_position", symbol=symbol)

    async def _confirm_or_cancel(self, symbol: str, ex: Position | None) -> None:
        tr = self.tracked[symbol]
        if ex is not None and ex.direction is tr.pos.direction:
            log.warning("engine.uncertain_entry_confirmed", symbol=symbol, trade_id=tr.trade_id)
            await self._confirm(symbol, ex.entry_price, ex.qty, tr.tp1_link_id)
            self._unmanaged_alerted.discard(symbol)
            return
        tr.confirm_attempts += 1
        if tr.confirm_attempts >= CONFIRM_ATTEMPTS:
            del self.tracked[symbol]
            await self.repo.update_trade(tr.trade_id, status="cancelled", close_reason="not_filled")
            log.warning("engine.uncertain_entry_cancelled", symbol=symbol, trade_id=tr.trade_id)

    async def _on_tp1(self, symbol: str, ex_qty: float, broker: BrokerAdapter) -> None:
        tr = self.tracked[symbol]
        pos = tr.pos
        pos.tp1_done = True
        pos.remaining = ex_qty
        inst = await broker.get_instrument(symbol)
        move = on_tp1_filled(pos, float(inst.taker_fee))
        await self.repo.update_trade(tr.trade_id, tp1_done=True, remaining_qty=Decimal(str(ex_qty)))
        if move is not None:
            await self._move_stop(symbol, move, broker)
        self.risk.update_open_risk(symbol, pos.open_risk())
        self.bus.publish("trade_updated", trade_id=tr.trade_id, symbol=symbol, tp1_done=True)

    async def _finalize(self, symbol: str, broker: BrokerAdapter) -> None:
        tr = self.tracked[symbol]
        pos = tr.pos
        closes = []
        try:
            closes = await broker.get_closed_pnl(symbol, pos.opened_ts - 5_000)
        except BrokerError as exc:
            log.warning("engine.closed_pnl_unavailable", symbol=symbol, error=str(exc))
        closed_qty = sum(float(c.qty) for c in closes)
        tr.finalize_attempts += 1
        if closed_qty < pos.qty * 0.999 and tr.finalize_attempts < FINALIZE_ATTEMPTS:
            return  # биржа ещё не отдала итог — дождёмся следующей сверки
        pnl = float(sum(c.pnl for c in closes))
        exit_price = (
            sum(float(c.avg_exit) * float(c.qty) for c in closes) / closed_qty
            if closed_qty
            else None
        )
        last_exit = float(closes[-1].avg_exit) if closes else None
        reason = tr.pending_reason or _infer_reason(pos, last_exit)
        r_mult = pnl / pos.risk_amount if pos.risk_amount > 0 else 0.0
        now = self.clock()
        await self.repo.update_trade(
            tr.trade_id,
            status="closed",
            remaining_qty=Decimal(0),
            realized_pnl=Decimal(str(pnl)),
            exit_price=Decimal(str(exit_price)) if exit_price is not None else None,
            r_multiple=r_mult,
            close_reason=reason.value,
            closed_at=ms_to_dt(now),
            bars_held=pos.bars_held,
        )
        del self.tracked[symbol]
        self.risk.on_position_closed(symbol)
        self.risk.on_trade_closed(now, pnl)
        self.bus.publish(
            "trade_closed",
            trade_id=tr.trade_id,
            symbol=symbol,
            pnl=pnl,
            r_multiple=r_mult,
            reason=reason.value,
            exit=exit_price,
        )
        log.info("engine.trade_closed", symbol=symbol, pnl=pnl, reason=reason.value)

    async def _snapshot_equity(self) -> None:
        equity = available = unreal = Decimal(0)
        accounts = {b.account_key(): b for b in self.brokers.values()}
        for key, broker in accounts.items():
            try:
                bal = await broker.get_balance()
            except BrokerError as exc:
                log.warning("engine.balance_unavailable", account=key, error=str(exc))
                self._api_error("balance_unavailable")
                return
            equity += bal.equity
            available += bal.available
        for market_name, broker in self.brokers.items():
            try:
                positions = await broker.get_positions()
            except BrokerError as exc:
                log.warning("engine.positions_unavailable", market=market_name, error=str(exc))
                return
            symbols = (
                set(self.markets[market_name].symbols) if market_name in self.markets else set()
            )
            # позиции одного счёта видны через адаптеры разных категорий: берём только свои
            unreal += sum(
                (p.unrealized_pnl for p in positions if not symbols or p.symbol in symbols),
                Decimal(0),
            )
        now = self.clock()
        self.risk.on_equity(now, float(equity))
        open_risk = Decimal(str(sum(t.pos.open_risk() for t in self.tracked.values())))
        await self.repo.save_equity(
            now // 60_000 * 60_000, equity, equity - unreal, unreal, open_risk
        )
        self.bus.publish(
            "equity",
            equity=float(equity),
            available=float(available),
            unrealized=float(unreal),
            open_risk=float(open_risk),
            drawdown_pct=round(self.risk.drawdown_pct(), 2),
        )

    async def _save_risk_state(self) -> None:
        await self.repo.set_state("risk", self.risk.to_dict())

    # ------------------------------------------------------------------ управление
    async def apply_config(self, config: TradingConfig) -> bool:
        """Применяет новые настройки риска и стратегии на лету.
        Возвращает True, если изменились инструменты/таймфреймы — нужен перезапуск."""
        async with self._lock:
            restart = {n: m.model_dump() for n, m in config.markets.items()} != {
                n: m.model_dump() for n, m in self.config.markets.items()
            }
            self.config = config
            self.signal_engine = SignalEngine(config.strategy)
            self.risk.settings = config.risk
            self.bus.publish("bot_status", **self.status())
            return restart

    def pause(self) -> None:
        self.paused = True
        self.bus.publish("bot_status", **self.status())

    def resume(self) -> None:
        self.paused = False
        self.breaker.reset()
        if self.risk.state.halted:
            self.risk.resume()
        self.bus.publish("bot_status", **self.status())

    async def kill_switch(self) -> None:
        """Отменить все ордера, закрыть все позиции, остановить новые входы."""
        log.warning("engine.kill_switch")
        async with self._lock:
            self.risk.halt(self.clock(), "kill_switch")
            self.paused = True
            for symbol in list(self.tracked):
                self.tracked[symbol].pending_reason = CloseReason.KILL
            for market_name, broker in {id(b): (m, b) for m, b in self.brokers.items()}.values():
                try:
                    await broker.cancel_all()
                    for p in await broker.get_positions():
                        await broker.close_position(p.symbol)
                except BrokerError as exc:
                    log.error("engine.kill_failed", market=market_name, error=str(exc))
                    self.bus.publish("alert", level="error", kind="kill_failed", error=str(exc))
            await self._reconcile_all()
        self.bus.publish("bot_status", **self.status())

    async def close_manually(self, symbol: str) -> None:
        async with self._lock:
            tr = self.tracked.get(symbol)
            if tr is not None and await self.close_trade(symbol, CloseReason.MANUAL):
                await self._reconcile_market(tr.market)

    async def move_to_breakeven(self, symbol: str) -> None:
        async with self._lock:
            tr = self.tracked.get(symbol)
            if tr is None:
                return
            broker = self.brokers[tr.market]
            inst = await broker.get_instrument(symbol)
            move = on_tp1_filled(tr.pos, float(inst.taker_fee))
            if move is not None:
                await self._move_stop(symbol, move, broker)


def _infer_reason(pos: ManagedPosition, exit_price: float | None) -> CloseReason:
    if exit_price is None:
        return pos.stop_kind.close_reason
    if abs(exit_price - pos.tp2) < abs(exit_price - pos.stop):
        return CloseReason.TP2
    return pos.stop_kind.close_reason


def _position_from_row(row: TradeRow, symbol: str) -> ManagedPosition:
    extra = row.extra or {}
    stop = float(row.stop_loss)
    initial = float(row.initial_stop)
    kind = StopKind.INITIAL
    if stop != initial:
        kind = StopKind.TRAILING if row.tp1_done and extra.get("trailing") else StopKind.BREAKEVEN
    return ManagedPosition(
        symbol=symbol,
        direction=Direction(row.direction),
        strategy=row.strategy,
        entry=float(row.entry_price or extra.get("plan_entry") or 0),
        qty=float(row.qty),
        initial_stop=initial,
        stop=stop,
        tp1=float(row.tp1) if row.tp1 is not None else None,
        tp2=float(row.tp2) if row.tp2 is not None else stop,
        tp1_fraction=float(extra.get("tp1_fraction", 0.0)),
        trailing=bool(extra.get("trailing", False)),
        opened_ts=int(row.opened_at.timestamp() * 1000),
        risk_amount=float(row.risk_amount),
        confidence=row.confidence,
        regime=str(extra.get("regime", "")),
        remaining=float(row.remaining_qty),
        tp1_done=row.tp1_done,
        stop_kind=kind,
        bars_held=row.bars_held,
    )
