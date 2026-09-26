"""Торговый движок для paper/live: портфель дневного тренда на споте.

Поток: закрылись дневные свечи всех монет → целевые доли (app.strategy.trend) → журнал
сигналов → в день ребалансировки (раз в неделю) рыночные ордера на разницу между текущей и
целевой долей: сначала продажи, потом покупки на освободившиеся USDT. Стопов нет: монета
продаётся, когда тренд по ней пропадает (доля → 0).

Каждое «владение» монетой (от первой покупки до полной продажи) ведётся как сделка в БД: со
средней ценой входа, докупками, частичными продажами и итоговым результатом. Сверка с биржей
(reconcile) раз в N секунд синхронизирует объёмы с кошельком, пишет капитал, следит за
просадкой и догоняет пропущенный дневной расчёт. Биржа — источник истины.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from app.brokers.base import BrokerAdapter, BrokerError, CandleClosed
from app.brokers.paper import PaperBroker
from app.core.events import EventBus
from app.db.candles import ms_to_dt
from app.db.models import TradeRow
from app.db.repo import TradeRepo
from app.domain import Candle, Direction, Timeframe
from app.execution.executor import OrderExecutor, OrderUncertain
from app.market.store import CandleStore
from app.risk.circuit import CircuitBreaker
from app.risk.manager import RiskEvent, RiskManager
from app.strategy import trend
from app.strategy.trend import Signal
from app.trading_config import MarketSettings, TradingConfig

log = structlog.get_logger()

DAY_MS = 86_400_000
TF = Timeframe.D1
# если к 00:15 UTC пришли не все дневные свечи — считаем по тем, что есть
EVAL_GRACE_MS = 15 * 60_000
BUY_CASH_BUFFER = 0.995  # запас на комиссию и проскальзывание при покупке
QTY_SYNC_TOLERANCE = 0.02  # расхождение с кошельком до 2% — молча подстраиваемся (комиссии)
STATE_KEY = "engine"


def wall_ms() -> int:
    return int(time.time() * 1000)


def weekday(ts_ms: int) -> int:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).weekday()


@dataclass
class Holding:
    trade_id: int
    market: str
    qty: float  # монет в кошельке
    avg_entry: float
    invested: float  # сумма покупок, USDT — база для доходности владения
    realized: float  # реализованный результат с учётом комиссий, USDT
    fees: float
    opened_ts: int


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
        self.risk = RiskManager(config.risk, on_event=self._on_risk_event)
        self.executors = {m: OrderExecutor(b, repo) for m, b in brokers.items()}
        self.markets: dict[str, MarketSettings] = {
            name: m for name, m in config.markets.items() if m.enabled and name in brokers
        }
        self.symbol_market = {s: name for name, m in self.markets.items() for s in m.symbols}
        self.holdings: dict[str, Holding] = {}
        self.targets: dict[str, float] = {}
        self.last_signal: dict[str, Signal] = {}
        self.signal_ids: dict[str, int] = {}  # id сигнала последнего расчёта — для сделки
        self.prices: dict[str, float] = {}
        self.last_eval: dict[str, int] = {}
        self.last_rebalance: dict[str, int] = {}
        self.paused = False
        self._seen: dict[tuple[str, int], set[str]] = {}
        self._lock = asyncio.Lock()
        self._unmanaged_alerted: set[str] = set()
        self._stale_alerted: set[tuple[str, int]] = set()
        self._bg: set[asyncio.Task[None]] = set()
        self.breaker = CircuitBreaker()

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        state = await self.repo.get_state("risk")
        if state:
            self.risk.state = RiskManager.state_from_dict(state)
        saved = await self.repo.get_state(STATE_KEY) or {}
        self.last_eval = {k: int(v) for k, v in saved.get("last_eval", {}).items()}
        self.last_rebalance = {k: int(v) for k, v in saved.get("last_rebalance", {}).items()}
        self.targets = {k: float(v) for k, v in saved.get("targets", {}).items()}
        self.paused = bool(saved.get("paused", False))
        for name, market in self.markets.items():
            prev = saved.get("symbols", {}).get(name)
            changed = (
                prev != market.symbols
                if prev is not None
                else bool(self.targets) and any(s not in self.targets for s in market.symbols)
            )
            if changed:
                # список монет изменился: доли считались по старому списку — пересчитать
                # (сделок это не вызывает, они — по расписанию или вручную)
                self.last_eval.pop(name, None)
        await self._load_prices()
        for row in await self.repo.open_trades():
            symbol = self._symbol_of(row)
            if symbol is None or symbol not in self.symbol_market:
                log.warning("engine.orphan_trade", trade_id=row.id)
                continue
            self.holdings[symbol] = _holding_from_row(row, self.symbol_market[symbol])
        await self.reconcile()
        self.bus.publish("bot_status", **self.status())

    async def _load_prices(self) -> None:
        """Последние цены — из сохранённых дневных свечей: после рестарта новых свечей по монете
        может не прийти до следующего дня, а без цены её нельзя ни оценить, ни торговать."""
        for symbol, name in self.symbol_market.items():
            if symbol in self.prices:
                continue
            candles = await self.store.get_candles(symbol, TF, limit=1)
            if candles:
                self._mark(name, symbol, candles[-1])

    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused,
            "halted": self.risk.state.halted,
            "halt_reason": self.risk.state.halt_reason,
            "open_positions": len(self.holdings),
            "drawdown_pct": round(self.risk.drawdown_pct(), 2),
            "circuit_breaker": self.breaker.tripped,
            "last_rebalance_ts": max(self.last_rebalance.values(), default=None),
            "next_rebalance_ts": self._next_rebalance_ts(),
            "target_vol_pct": self.config.risk.target_vol_pct,
        }

    def _next_rebalance_ts(self) -> int:
        """Начало ближайшего дня rebalance_weekday (UTC): тогда закроется нужная свеча."""
        today = self.clock() // DAY_MS * DAY_MS
        for k in range(1, 8):
            ts = today + k * DAY_MS
            if weekday(ts) == self.config.strategy.rebalance_weekday:
                return ts
        return today + 7 * DAY_MS

    def _symbol_of(self, row: TradeRow) -> str | None:
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
        if market_name is None or event.timeframe is not TF:
            return
        self._mark(market_name, event.symbol, event.candle)
        day = event.candle.ts
        seen = self._seen.setdefault((market_name, day), set())
        seen.add(event.symbol)
        if seen >= set(self.markets[market_name].symbols):
            self._seen = {k: v for k, v in self._seen.items() if k[1] > day}
            async with self._lock:
                await self._evaluate(market_name, day)

    async def on_backfill(self, symbol: str, tf: Timeframe, candles: list[Candle]) -> None:
        """Свечи, докачанные после старта/обрыва: обновляем последнюю цену."""
        market_name = self.symbol_market.get(symbol)
        if market_name is not None and tf is TF and candles:
            self._mark(market_name, symbol, candles[-1])

    def _mark(self, market_name: str, symbol: str, candle: Candle) -> None:
        self.prices[symbol] = candle.close
        broker = self.brokers[market_name]
        if isinstance(broker, PaperBroker):
            broker.mark_price(symbol, candle.close, candle.ts + DAY_MS)

    async def _maybe_catch_up(self) -> None:
        """Дневной расчёт пропущен (не пришли все свечи, рестарт) — делаем его сейчас."""
        now = self.clock()
        day = now // DAY_MS * DAY_MS - DAY_MS  # последняя закрытая дневная свеча
        if now < day + DAY_MS + EVAL_GRACE_MS:
            return
        for name, market in self.markets.items():
            if self.last_eval.get(name, -1) >= day:
                continue
            if self.ensure_fresh is not None:
                for s in market.symbols:
                    await self.ensure_fresh(s, TF)
            await self._evaluate(name, day)

    # ------------------------------------------------------------------ сигналы
    async def _closes(self, market: MarketSettings, day: int) -> pd.DataFrame:
        limit = self.config.strategy.history_days + 60
        series = {}
        for s in market.symbols:
            candles = await self.store.get_candles(s, TF, end_ms=day, limit=limit)
            series[s] = pd.Series(
                [c.close for c in candles], index=[c.ts for c in candles], dtype="float64"
            )
        return pd.DataFrame(series).sort_index()

    async def _evaluate(self, market_name: str, day: int) -> None:
        if day <= self.last_eval.get(market_name, -1):
            return
        market = self.markets[market_name]
        closes = await self._closes(market, day)
        missing = [
            s
            for s in market.symbols
            if s not in closes or day not in closes.index or pd.isna(closes.at[day, s])
        ]
        if closes.empty or missing:
            # свеча дня есть не у всех монет (история ещё не докачана): считать по старым
            # данным нельзя — расчёт повторится на следующей сверке
            log.warning("engine.stale_data", market=market_name, day=day, missing=missing)
            if (market_name, day) not in self._stale_alerted:
                self._stale_alerted.add((market_name, day))
                self.bus.publish("alert", level="warning", kind="stale_data", missing=missing)
            return
        for s in market.symbols:
            last = closes[s].dropna()
            if not last.empty:
                self.prices[s] = float(last.iloc[-1])
        signals = trend.latest_signals(
            closes, market.symbols, self.config.strategy, self.config.risk
        )
        self.last_eval[market_name] = day
        # сделки — только в день ребалансировки или при первом запуске
        due = (
            weekday(day + DAY_MS) == self.config.strategy.rebalance_weekday
            or market_name not in self.last_rebalance
        )
        reason = None
        if not due:
            reason = "not_rebalance_day"
        elif self.risk.state.halted:
            reason = f"halted:{self.risk.state.halt_reason}"
        elif self.paused:
            reason = "paused"
        for sg in signals:
            self.targets[sg.symbol] = sg.weight
            self.last_signal[sg.symbol] = sg
            iid = self.instrument_ids.get(sg.symbol)
            if iid is not None:
                self.signal_ids[sg.symbol] = await self.repo.save_signal(
                    iid, sg, reason is None, reason
                )
            self.bus.publish(
                "signal", symbol=sg.symbol, weight=sg.weight, score=sg.score, rebalance=due
            )
        if due:
            await self._rebalance(market_name, day, "schedule")
        await self._save_state()

    # ------------------------------------------------------------------ ребалансировка
    async def _rebalance(self, market_name: str, day: int, reason: str) -> int | str:
        """Число отправленных ордеров или причина, по которой ребалансировка не выполнена."""
        if self.paused or self.risk.state.halted:
            log.info("engine.rebalance_skipped", market=market_name, paused=self.paused)
            self.bus.publish("rebalance", market=market_name, skipped=True, reason="paused")
            return "halted" if self.risk.state.halted else "paused"
        market = self.markets[market_name]
        broker = self.brokers[market_name]
        try:
            wallet = {p.symbol: float(p.qty) for p in await broker.get_positions()}
            cash = float((await broker.get_balance()).available)
        except BrokerError as exc:
            log.error("engine.rebalance_failed", market=market_name, error=str(exc))
            self._api_error("rebalance_failed", market=market_name)
            return "broker_error"
        # капитал и текущие доли — только по монетам, которые ведёт бот; чужие монеты на счёте
        # не учитываются и не продаются
        held_qty = {s: wallet.get(s, 0.0) if s in self.holdings else 0.0 for s in market.symbols}
        capital = cash + sum(q * self.prices.get(s, 0.0) for s, q in held_qty.items())
        min_trade = self.config.strategy.min_trade_pct / 100 * capital
        # id ордеров должны быть уникальны: плановая — по дню, внеплановая — по времени
        if reason == "schedule":
            tag = f"r{datetime.fromtimestamp(day / 1000, tz=UTC):%y%m%d}"
        else:
            tag = f"{reason[0]}{self.clock() // 1000 % 10**8}"
        sells: list[tuple[str, float | None]] = []
        buys: list[tuple[str, float]] = []
        for s in market.symbols:
            price = self.prices.get(s)
            if not price:
                continue
            held = held_qty[s]
            target = self.targets.get(s, 0.0)
            diff = target * capital - held * price
            if target == 0 and held > 0:
                sells.append((s, None))  # выход целиком, без порога
            elif diff <= -min_trade and diff < 0:
                sells.append((s, -diff / price))
            elif diff >= min_trade and diff > 0:
                buys.append((s, diff / price))
        orders: list[dict[str, Any]] = []
        for s, qty in sells:
            if not await self._sell(market_name, s, qty, held_qty[s], tag, reason, orders):
                break
        else:
            if buys:
                try:
                    cash = float((await broker.get_balance()).available)
                except BrokerError as exc:
                    log.error("engine.balance_unavailable", error=str(exc))
                    self._api_error("balance_unavailable")
                    cash = 0.0
                need = sum(q * self.prices[s] for s, q in buys)
                scale = min(1.0, cash * BUY_CASH_BUFFER / need) if need > 0 else 0.0
                for s, qty in buys:
                    if not await self._buy(market_name, s, qty * scale, tag, orders):
                        break
        self.last_rebalance[market_name] = day
        self.bus.publish("rebalance", market=market_name, reason=reason, orders=orders)
        log.info("engine.rebalanced", market=market_name, orders=len(orders))
        return len(orders)

    async def _buy(
        self, market_name: str, symbol: str, qty: float, tag: str, orders: list[dict[str, Any]]
    ) -> bool:
        """Возвращает False, если продолжать ребалансировку нельзя."""
        broker = self.brokers[market_name]
        inst = await broker.get_instrument(symbol)
        q = inst.round_qty(qty)
        price = Decimal(str(self.prices[symbol]))
        if q <= 0 or q < inst.min_qty or q * price < inst.min_notional:
            return True
        h = self.holdings.get(symbol)
        now = self.clock()
        if h is None:
            trade_id = await self.repo.create_trade(
                instrument_id=self.instrument_ids[symbol],
                signal_id=self.signal_ids.get(symbol),
                strategy=trend.STRATEGY_NAME,
                direction=Direction.LONG.value,
                status="open",
                qty=Decimal(0),
                remaining_qty=Decimal(0),
                initial_stop=None,
                stop_loss=None,
                risk_amount=None,
                confidence=round(self.targets.get(symbol, 0.0) * 100, 4),
                opened_at=ms_to_dt(now),
                extra={"target_weight": self.targets.get(symbol, 0.0)},
            )
            h = Holding(trade_id, market_name, 0.0, 0.0, 0.0, 0.0, 0.0, now)
            self.holdings[symbol] = h
        try:
            fill = await self.executors[market_name].market_order(
                trade_id=h.trade_id,
                purpose=f"b{tag}",
                symbol=symbol,
                direction=Direction.LONG,
                qty=q,
            )
        except BrokerError as exc:
            return await self._order_failed(symbol, h, exc)
        px, fq = float(fill.price), float(fill.qty)
        fee = px * fq * float(inst.taker_fee)
        first = h.qty == 0
        h.avg_entry = (h.avg_entry * h.qty + px * fq) / (h.qty + fq)
        h.qty += fq
        h.invested += px * fq
        h.fees += fee
        h.realized -= fee
        await self._save_holding(symbol, h)
        orders.append({"symbol": symbol, "side": "buy", "qty": fq, "price": px})
        self.bus.publish(
            "trade_opened" if first else "trade_updated",
            trade_id=h.trade_id,
            symbol=symbol,
            side="buy",
            qty=fq,
            price=px,
            holding=h.qty,
            weight=self.targets.get(symbol, 0.0),
        )
        return True

    async def _sell(
        self,
        market_name: str,
        symbol: str,
        qty: float | None,
        wallet_qty: float,
        tag: str,
        reason: str,
        orders: list[dict[str, Any]],
    ) -> bool:
        """qty=None — продать всё. Возвращает False, если продолжать ребалансировку нельзя."""
        broker = self.brokers[market_name]
        inst = await broker.get_instrument(symbol)
        h = self.holdings.get(symbol)
        q = inst.round_qty(min(qty, wallet_qty) if qty is not None else wallet_qty)
        price = Decimal(str(self.prices.get(symbol, 0.0)))
        if q <= 0 or q < inst.min_qty or q * price < inst.min_notional:
            if qty is None and h is not None:
                # остался «пыльный» остаток меньше минимального ордера — владение закончено
                await self._close_holding(symbol, self.prices.get(symbol, h.avg_entry), reason)
            return True
        if h is None:
            self._alert_unmanaged(symbol)  # чужие монеты бот не продаёт
            return True
        try:
            fill = await self.executors[market_name].market_order(
                trade_id=h.trade_id,
                purpose=f"s{tag}",
                symbol=symbol,
                direction=Direction.SHORT,
                qty=q,
                reduce_only=True,
            )
        except BrokerError as exc:
            return await self._order_failed(symbol, h, exc)
        px, fq = float(fill.price), float(fill.qty)
        fee = px * fq * float(inst.taker_fee)
        h.realized += (px - h.avg_entry) * fq - fee
        h.fees += fee
        h.qty = max(0.0, h.qty - fq)
        orders.append({"symbol": symbol, "side": "sell", "qty": fq, "price": px})
        if qty is None or inst.round_qty(h.qty) < inst.min_qty:
            await self._close_holding(symbol, px, reason)
        else:
            await self._save_holding(symbol, h)
            self.bus.publish(
                "trade_updated",
                trade_id=h.trade_id,
                symbol=symbol,
                side="sell",
                qty=fq,
                price=px,
                holding=h.qty,
                weight=self.targets.get(symbol, 0.0),
            )
        return True

    async def _order_failed(self, symbol: str, h: Holding, exc: BrokerError) -> bool:
        uncertain = isinstance(exc, OrderUncertain)
        log.error("engine.order_failed", symbol=symbol, error=str(exc), uncertain=uncertain)
        self._api_error("order_failed", symbol=symbol)
        self.bus.publish("alert", level="error", kind="order_failed", symbol=symbol, error=str(exc))
        if h.qty == 0 and not uncertain:
            # покупка не состоялась — пустое владение не нужно
            del self.holdings[symbol]
            await self.repo.update_trade(h.trade_id, status="cancelled", close_reason="rejected")
        # исход неизвестен — остальные ордера не отправляем, сверка подтянет объём
        return not uncertain

    async def _save_holding(self, symbol: str, h: Holding) -> None:
        await self.repo.update_trade(
            h.trade_id,
            entry_price=Decimal(str(round(h.avg_entry, 10))),
            qty=Decimal(str(max(h.qty, 0.0))),
            remaining_qty=Decimal(str(max(h.qty, 0.0))),
            realized_pnl=Decimal(str(round(h.realized, 8))),
            fees=Decimal(str(round(h.fees, 8))),
            extra={
                "invested": round(h.invested, 8),
                "target_weight": self.targets.get(symbol, 0.0),
            },
        )

    async def _close_holding(self, symbol: str, exit_price: float, reason: str) -> None:
        h = self.holdings.pop(symbol)
        now = self.clock()
        ret = h.realized / h.invested if h.invested > 0 else 0.0
        await self.repo.update_trade(
            h.trade_id,
            status="closed",
            remaining_qty=Decimal(0),
            realized_pnl=Decimal(str(round(h.realized, 8))),
            fees=Decimal(str(round(h.fees, 8))),
            exit_price=Decimal(str(exit_price)),
            r_multiple=ret,  # для стратегии тренда — доходность владения (доля вложенного)
            close_reason=reason,
            closed_at=ms_to_dt(now),
            bars_held=max(0, (now - h.opened_ts) // DAY_MS),
            extra={"invested": round(h.invested, 8), "return_pct": round(ret * 100, 3)},
        )
        self.bus.publish(
            "trade_closed",
            trade_id=h.trade_id,
            symbol=symbol,
            pnl=h.realized,
            return_pct=round(ret * 100, 2),
            reason=reason,
            exit=exit_price,
        )
        log.info("engine.holding_closed", symbol=symbol, pnl=h.realized, reason=reason)

    def _alert_unmanaged(self, symbol: str) -> None:
        if symbol not in self._unmanaged_alerted:
            self._unmanaged_alerted.add(symbol)
            self.bus.publish("alert", level="warning", kind="unmanaged_holding", symbol=symbol)

    # ------------------------------------------------------------------ сверка
    async def reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_all()

    async def _reconcile_all(self) -> None:
        for market_name in self.markets:
            await self._reconcile_market(market_name)
        if await self._snapshot_equity():
            await self._liquidate("drawdown_stop")
        await self._maybe_catch_up()
        await self.repo.set_state("risk", self.risk.to_dict())

    async def _reconcile_market(self, market_name: str) -> None:
        broker = self.brokers[market_name]
        try:
            wallet = {p.symbol: float(p.qty) for p in await broker.get_positions()}
        except BrokerError as exc:
            log.error("engine.reconcile_failed", market=market_name, error=str(exc))
            self._api_error("reconcile_failed", market=market_name)
            return
        for symbol, h in list(self.holdings.items()):
            if h.market != market_name:
                continue
            ex = wallet.get(symbol, 0.0)
            inst = await broker.get_instrument(symbol)
            if h.qty == 0 and h.invested == 0:
                # покупка с неизвестным исходом (обрыв связи): монеты пришли или нет
                if ex < float(inst.min_qty):
                    del self.holdings[symbol]
                    await self.repo.update_trade(
                        h.trade_id, status="cancelled", close_reason="not_filled"
                    )
                    continue
                price = self.prices.get(symbol, 0.0)
                h.qty, h.avg_entry, h.invested = ex, price, ex * price  # цена — оценка
                self.bus.publish(
                    "alert", level="warning", kind="uncertain_buy_filled", symbol=symbol, qty=ex
                )
                await self._save_holding(symbol, h)
                continue
            if ex < float(inst.min_qty):
                # монет больше нет (продали вручную на бирже)
                self.bus.publish("alert", level="warning", kind="holding_gone", symbol=symbol)
                await self._close_holding(symbol, self.prices.get(symbol, h.avg_entry), "external")
            elif abs(ex - h.qty) > QTY_SYNC_TOLERANCE * h.qty:
                self.bus.publish(
                    "alert",
                    level="warning",
                    kind="holding_changed",
                    symbol=symbol,
                    tracked=h.qty,
                    wallet=ex,
                )
                h.qty = ex
                await self._save_holding(symbol, h)
            elif ex != h.qty:
                h.qty = ex  # комиссия покупки на споте Bybit списывается в монете
                await self._save_holding(symbol, h)
        for symbol in self.markets[market_name].symbols:
            qty = wallet.get(symbol, 0.0)
            if symbol not in self.holdings and qty > 0:
                inst = await broker.get_instrument(symbol)
                if qty >= float(inst.min_qty):
                    self._alert_unmanaged(symbol)

    async def _snapshot_equity(self) -> bool:
        """Капитал стратегии = свободные USDT + монеты из списка. Другие активы счёта не
        учитываются и не торгуются. True — сработала остановка по просадке."""
        cash = Decimal(0)
        for market_name, broker in self.brokers.items():
            try:
                cash += (await broker.get_balance()).available
            except BrokerError as exc:
                log.warning("engine.balance_unavailable", market=market_name, error=str(exc))
                self._api_error("balance_unavailable")
                return False
        invested = unreal = Decimal(0)
        for symbol, h in self.holdings.items():
            price = Decimal(str(self.prices.get(symbol, h.avg_entry)))
            qty = Decimal(str(h.qty))
            invested += qty * price
            unreal += (price - Decimal(str(h.avg_entry))) * qty
        capital = cash + invested
        now = self.clock()
        halted_now = self.risk.on_equity(now, float(capital))
        await self.repo.save_equity(
            now // 60_000 * 60_000, capital, capital - unreal, unreal, invested
        )
        self.bus.publish(
            "equity",
            equity=float(capital),
            unrealized=float(unreal),
            invested=float(invested),
            drawdown_pct=round(self.risk.drawdown_pct(), 2),
        )
        return halted_now

    async def _save_state(self) -> None:
        await self.repo.set_state(
            STATE_KEY,
            {
                "last_eval": self.last_eval,
                "last_rebalance": self.last_rebalance,
                "targets": self.targets,
                "paused": self.paused,
                "symbols": {n: list(m.symbols) for n, m in self.markets.items()},
            },
        )

    # ------------------------------------------------------------------ управление
    async def _liquidate(self, reason: str) -> None:
        """Продать все монеты стратегии в USDT и приостановить торговлю."""
        self.paused = True
        tag = f"x{self.clock() // 1000 % 10**8}"
        orders: list[dict[str, Any]] = []
        for symbol, h in list(self.holdings.items()):
            broker = self.brokers[h.market]
            try:
                wallet = {p.symbol: float(p.qty) for p in await broker.get_positions()}
            except BrokerError as exc:
                log.error("engine.liquidate_positions_failed", error=str(exc))
                wallet = {symbol: h.qty}
            await self._sell(h.market, symbol, None, wallet.get(symbol, h.qty), tag, reason, orders)
        await self._save_state()
        self.bus.publish("rebalance", reason=reason, orders=orders)
        self.bus.publish("bot_status", **self.status())

    async def apply_config(self, config: TradingConfig) -> bool:
        """Применяет настройки риска и стратегии на лету (действуют с ближайшего расчёта).
        Возвращает True, если изменился список монет — нужен перезапуск."""
        async with self._lock:
            restart = {n: m.model_dump() for n, m in config.markets.items()} != {
                n: m.model_dump() for n, m in self.config.markets.items()
            }
            self.config = config
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
        """Продать все монеты стратегии, остановить торговлю."""
        log.warning("engine.kill_switch")
        async with self._lock:
            self.risk.halt(self.clock(), "kill_switch")
            await self._liquidate("kill")
            await self._reconcile_all()
        self.bus.publish("bot_status", **self.status())

    async def close_manually(self, symbol: str) -> None:
        """Продать монету сейчас. На следующей ребалансировке стратегия решит заново."""
        async with self._lock:
            h = self.holdings.get(symbol)
            if h is None:
                return
            broker = self.brokers[h.market]
            wallet = {p.symbol: float(p.qty) for p in await broker.get_positions()}
            tag = f"m{self.clock() // 1000 % 10**8}"
            await self._sell(h.market, symbol, None, wallet.get(symbol, 0.0), tag, "manual", [])

    async def rebalance_now(self) -> dict[str, int | str]:
        """Внеплановая ребалансировка к последним рассчитанным долям. По каждому рынку —
        число ордеров или причина пропуска (targets_pending — доли ещё не рассчитаны)."""
        async with self._lock:
            await self._load_prices()
            results: dict[str, int | str] = {}
            for name in self.markets:
                day = self.last_eval.get(name)
                if day is None:
                    results[name] = "targets_pending"
                    continue
                results[name] = await self._rebalance(name, day, "manual")
            await self._save_state()
            return results


def _holding_from_row(row: TradeRow, market: str) -> Holding:
    extra = row.extra or {}
    qty = float(row.remaining_qty)
    avg = float(row.entry_price or 0)
    return Holding(
        trade_id=row.id,
        market=market,
        qty=qty,
        avg_entry=avg,
        invested=float(extra.get("invested", qty * avg)),
        realized=float(row.realized_pnl),
        fees=float(row.fees),
        opened_ts=int(row.opened_at.timestamp() * 1000),
    )
