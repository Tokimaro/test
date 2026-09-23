"""Адаптер Alpaca (акции США) — REST API v2.

Документация: https://docs.alpaca.markets/reference

Особенности по сравнению с Bybit:
* нет стопа «на позиции»: защита ставится OCO-парами (стоп + тейк) на объём позиции.
  Запрос на TP1 (reduce-only лимитка) делит защиту на две OCO: TP1-часть и остаток —
  так стоп всегда покрывает весь объём, а объём не резервируется дважды;
* свечи опрашиваются через REST (поток минутных баров пришлось бы агрегировать);
* торговля только в регулярную сессию (календарь с праздниками — /v2/calendar);
* целые акции (bracket/OCO не поддерживают дробные объёмы), комиссий нет.

⚠ Адаптер проверен тестами на документированных ответах API. Перед реальными деньгами
обязательно проверьте его на paper-счёте Alpaca.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.brokers.base import (
    BrokerAdapter,
    BrokerError,
    CandleClosed,
    ClosedPnl,
    OrderRequest,
    OrderResult,
    OrderType,
    StreamEvent,
    StreamReconnected,
)
from app.domain import Balance, Candle, Direction, Instrument, MarketType, Position, Timeframe
from app.market.sessions import SessionCalendar, parse_day

log = structlog.get_logger()

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

TIMEFRAMES = {
    Timeframe.M1: "1Min",
    Timeframe.M5: "5Min",
    Timeframe.M15: "15Min",
    Timeframe.M30: "30Min",
    Timeframe.H1: "1Hour",
    Timeframe.H4: "4Hour",
    Timeframe.D1: "1Day",
}
STOP_TYPES = {"stop", "stop_limit", "trailing_stop"}


def _dec(v: Any, default: str = "0") -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else Decimal(default)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class AlpacaClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        max_retries: int = 3,
        backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=transport,
        )
        self._retries = max_retries
        self._backoff = backoff_s

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt > self._retries:
                    raise BrokerError(f"{method} {path}: сеть недоступна: {exc}") from exc
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self._retries:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    code, msg = body.get("code"), body.get("message")
                except ValueError:
                    code, msg = None, resp.text[:200]
                raise BrokerError(
                    f"{method} {path}: HTTP {resp.status_code}: {msg}",
                    code=int(code) if code else resp.status_code,
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()


class AlpacaAdapter(BrokerAdapter):
    name = "alpaca"

    def __init__(
        self,
        trading: AlpacaClient,
        data: AlpacaClient,
        *,
        paper: bool = True,
        feed: str = "iex",
        session_buffer_minutes: int = 0,
        poll_interval_s: float = 30.0,
        fill_timeout_s: float = 10.0,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._t = trading
        self._d = data
        self._paper = paper
        self._feed = feed
        self._buffer = session_buffer_minutes
        self._poll = poll_interval_s
        self._fill_timeout = fill_timeout_s
        self._clock = clock
        self.calendar = SessionCalendar()
        self._calendar_loaded: date | None = None
        self._instruments: dict[str, Instrument] = {}

    def market_type(self) -> MarketType:
        return MarketType.STOCK

    def account_key(self) -> str:
        return f"alpaca:{'paper' if self._paper else 'live'}:{self._t.api_key}"

    async def aclose(self) -> None:
        await self._t.aclose()
        await self._d.aclose()

    # ------------------------------------------------------------------ сессии
    async def refresh_calendar(self) -> None:
        today = datetime.fromtimestamp(self._clock() / 1000, tz=UTC).date()
        if self._calendar_loaded == today:
            return
        days = await self._t.request(
            "GET",
            "/v2/calendar",
            params={
                "start": (today - timedelta(days=10)).isoformat(),
                "end": (today + timedelta(days=30)).isoformat(),
            },
        )
        self.calendar.load([parse_day(d["date"], d["open"], d["close"]) for d in days or []])
        self._calendar_loaded = today

    def is_market_open(self, symbol: str, ts_ms: int) -> bool:
        return self.calendar.is_open(ts_ms, self._buffer)

    async def server_time_ms(self) -> int:
        clock = await self._t.request("GET", "/v2/clock")
        return _ms(clock["timestamp"])

    # ------------------------------------------------------------------ данные
    async def get_instrument(self, symbol: str) -> Instrument:
        if symbol in self._instruments:
            return self._instruments[symbol]
        asset = await self._t.request("GET", f"/v2/assets/{symbol}", allow_404=True)
        if asset is None or not asset.get("tradable"):
            raise BrokerError(f"{symbol}: инструмент не найден или не торгуется")
        inst = Instrument(
            symbol=symbol,
            market_type=MarketType.STOCK,
            category="stock",
            tick_size=Decimal("0.01"),
            qty_step=Decimal(1),
            min_qty=Decimal(1),
            max_qty=Decimal(1_000_000),
            min_notional=Decimal(1),
            max_leverage=Decimal(1),
            taker_fee=Decimal(0),
            maker_fee=Decimal(0),
        )
        self._instruments[symbol] = inst
        return inst

    async def get_candles(
        self, symbol: str, timeframe: Timeframe, start_ms: int, end_ms: int
    ) -> list[Candle]:
        await self.refresh_calendar()
        out: dict[int, Candle] = {}
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "timeframe": TIMEFRAMES[timeframe],
                "start": _iso(start_ms),
                "end": _iso(end_ms),
                "limit": 10_000,
                "adjustment": "raw",
                "feed": self._feed,
            }
            if token:
                params["page_token"] = token
            body = await self._d.request("GET", f"/v2/stocks/{symbol}/bars", params=params)
            for b in body.get("bars") or []:
                c = Candle(
                    ts=_ms(b["t"]),
                    open=float(b["o"]),
                    high=float(b["h"]),
                    low=float(b["l"]),
                    close=float(b["c"]),
                    volume=float(b["v"]),
                )
                if start_ms <= c.ts <= end_ms:
                    out[c.ts] = c
            token = body.get("next_page_token")
            if not token:
                break
        return [out[k] for k in sorted(out)]

    async def stream_candles(
        self, subscriptions: list[tuple[str, Timeframe]]
    ) -> AsyncGenerator[StreamEvent]:
        """Опрос REST: раз в poll_interval — последние закрытые бары (дубли отсеет CandleFeed)."""
        failing = False
        while True:
            now = self._clock()
            try:
                for symbol, tf in subscriptions:
                    bars = await self.get_candles(symbol, tf, now - 3 * tf.ms, now)
                    for c in bars:
                        if c.ts + tf.ms <= now:
                            yield CandleClosed(symbol, tf, c)
                if failing:
                    failing = False
                    yield StreamReconnected()
            except BrokerError as exc:
                failing = True
                log.warning("alpaca.poll_failed", error=str(exc))
            await asyncio.sleep(self._poll)

    # ------------------------------------------------------------------ счёт
    async def get_balance(self) -> Balance:
        acc = await self._t.request("GET", "/v2/account")
        equity = _dec(acc.get("equity"))
        # без плеча: доступно не больше капитала, даже если маржинальный счёт позволяет больше
        available = min(_dec(acc.get("buying_power")), equity)
        return Balance(equity=equity, available=max(Decimal(0), available), currency="USD")

    async def _open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"status": "open", "limit": 500, "nested": "false"}
        if symbol:
            params["symbols"] = symbol
        return list(await self._t.request("GET", "/v2/orders", params=params) or [])

    async def get_positions(self) -> list[Position]:
        raw = await self._t.request("GET", "/v2/positions") or []
        if not raw:
            return []
        orders = await self._open_orders()
        out = []
        for p in raw:
            symbol = p["symbol"]
            direction = Direction.LONG if p.get("side") == "long" else Direction.SHORT
            exit_side = "sell" if direction is Direction.LONG else "buy"
            mine = [o for o in orders if o["symbol"] == symbol and o["side"] == exit_side]
            stops = [_dec(o["stop_price"]) for o in mine if o.get("type") in STOP_TYPES]
            limits = [_dec(o["limit_price"]) for o in mine if o.get("type") == "limit"]
            # стоп-ордера должны покрывать всю позицию, иначе считаем, что стопа нет
            stop_qty = sum(_dec(o["qty"]) for o in mine if o.get("type") in STOP_TYPES)
            qty = abs(_dec(p["qty"]))
            far_tp = (
                (max(limits) if direction is Direction.LONG else min(limits)) if limits else None
            )
            out.append(
                Position(
                    symbol=symbol,
                    direction=direction,
                    qty=qty,
                    entry_price=_dec(p.get("avg_entry_price")),
                    stop_loss=stops[0] if stops and stop_qty >= qty else None,
                    take_profit=far_tp,
                    unrealized_pnl=_dec(p.get("unrealized_pl")),
                    leverage=Decimal(1),
                )
            )
        return out

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        return None  # у акций плечо задаётся типом счёта, а не ордером

    # ------------------------------------------------------------------ ордера
    async def place_order(self, req: OrderRequest) -> OrderResult:
        if req.qty <= 0 or req.qty != req.qty.to_integral_value():
            raise BrokerError("объём акций должен быть целым и > 0")
        side = "buy" if req.direction is Direction.LONG else "sell"
        if req.order_type is OrderType.LIMIT and req.reduce_only:
            return await self._split_take_profit(req)
        body: dict[str, Any] = {
            "symbol": req.symbol,
            "qty": str(req.qty),
            "side": side,
            "type": "market" if req.order_type is OrderType.MARKET else "limit",
            "time_in_force": "day",
            "client_order_id": req.link_id,
        }
        if req.order_type is OrderType.LIMIT:
            body["limit_price"] = str(req.price)
        order = await self._t.request("POST", "/v2/orders", json=body)
        result = _order_result(order)
        if req.stop_loss is not None and not req.reduce_only:
            filled = await self._await_fill(order["id"])
            await self._protect(
                req.symbol,
                req.direction,
                filled.filled_qty,
                req.stop_loss,
                req.take_profit,
                req.link_id,
            )
            result = filled
        return result

    async def _await_fill(self, order_id: str) -> OrderResult:
        deadline = time.monotonic() + self._fill_timeout
        while True:
            order = await self._t.request("GET", f"/v2/orders/{order_id}")
            if order["status"] == "filled":
                return _order_result(order)
            if order["status"] in ("canceled", "expired", "rejected"):
                raise BrokerError(f"ордер {order_id}: {order['status']}")
            if time.monotonic() > deadline:
                raise BrokerError(f"ордер {order_id} не исполнился за {self._fill_timeout}s")
            await asyncio.sleep(0.5)

    async def _protect(
        self,
        symbol: str,
        direction: Direction,
        qty: Decimal,
        stop: Decimal,
        take_profit: Decimal | None,
        link_prefix: str,
    ) -> None:
        """OCO: стоп + тейк на qty. Без тейка — одиночный стоп-ордер."""
        side = "sell" if direction is Direction.LONG else "buy"
        body: dict[str, Any] = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "time_in_force": "gtc",
            "client_order_id": f"{link_prefix}-p{int(time.time() * 1000) % 10**8}",
        }
        if take_profit is not None:
            body |= {
                "type": "limit",
                "order_class": "oco",
                "take_profit": {"limit_price": str(take_profit)},
                "stop_loss": {"stop_price": str(stop)},
            }
        else:
            body |= {"type": "stop", "stop_price": str(stop)}
        await self._t.request("POST", "/v2/orders", json=body)

    async def _split_take_profit(self, req: OrderRequest) -> OrderResult:
        """TP1: пересобрать защиту в две OCO — (TP1, стоп) на req.qty и (TP2, стоп) на остаток."""
        positions = {p.symbol: p for p in await self.get_positions()}
        pos = positions.get(req.symbol)
        if pos is None:
            raise BrokerError(f"{req.symbol}: нет позиции для TP1")
        orders = await self._open_orders(req.symbol)
        stop = next((_dec(o["stop_price"]) for o in orders if o.get("type") in STOP_TYPES), None)
        if stop is None:
            raise BrokerError(f"{req.symbol}: нет стопа — TP1 не выставляется")
        await self.cancel_all(req.symbol)
        rest = pos.qty - req.qty
        await self._protect(req.symbol, pos.direction, req.qty, stop, req.price, req.link_id)
        if rest > 0:
            await self._protect(req.symbol, pos.direction, rest, stop, pos.take_profit, req.link_id)
        return OrderResult(order_id=req.link_id, link_id=req.link_id, status="New")

    async def get_order(self, symbol: str, link_id: str) -> OrderResult | None:
        order = await self._t.request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": link_id},
            allow_404=True,
        )
        return _order_result(order) if order else None

    async def get_closed_pnl(self, symbol: str, since_ms: int) -> list[ClosedPnl]:
        fills = (
            await self._t.request(
                "GET",
                "/v2/account/activities/FILL",
                params={"after": _iso(since_ms), "direction": "asc", "page_size": 100},
            )
            or []
        )
        qty = Decimal(0)  # со знаком: >0 лонг
        avg = Decimal(0)
        out: list[ClosedPnl] = []
        for f in fills:
            if f.get("symbol") != symbol:
                continue
            q = _dec(f["qty"]) * (1 if f["side"] == "buy" else -1)
            price = _dec(f["price"])
            if qty == 0 or (qty > 0) == (q > 0):
                avg = (avg * abs(qty) + price * abs(q)) / (abs(qty) + abs(q))
                qty += q
                continue
            closed = min(abs(q), abs(qty))
            sign = 1 if qty > 0 else -1
            out.append(
                ClosedPnl(
                    symbol=symbol,
                    qty=closed,
                    avg_entry=avg,
                    avg_exit=price,
                    pnl=(price - avg) * sign * closed,
                    ts=_ms(f["transaction_time"]),
                    order_id=str(f.get("order_id", "")),
                )
            )
            qty += q
            if qty == 0:
                avg = Decimal(0)
        return out

    async def amend_stops(
        self,
        symbol: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        if stop_loss is None and take_profit is None:
            return
        orders = await self._open_orders(symbol)
        stops = [o for o in orders if o.get("type") in STOP_TYPES]
        if stop_loss is not None and stops:
            try:
                for o in stops:
                    await self._t.request(
                        "PATCH", f"/v2/orders/{o['id']}", json={"stop_price": str(stop_loss)}
                    )
                if take_profit is None:
                    return
            except BrokerError as exc:
                log.warning("alpaca.patch_stop_failed", symbol=symbol, error=str(exc))
        # нет стопов (восстановление) или замена не удалась — пересобираем защиту целиком
        positions = {p.symbol: p for p in await self.get_positions()}
        pos = positions.get(symbol)
        if pos is None:
            raise BrokerError(f"нет позиции {symbol}")
        stop = stop_loss if stop_loss is not None else pos.stop_loss
        if stop is None:
            raise BrokerError(f"{symbol}: неизвестен уровень стопа")
        await self.cancel_all(symbol)
        await self._protect(
            symbol, pos.direction, pos.qty, stop, take_profit or pos.take_profit, f"amend-{symbol}"
        )

    async def close_position(self, symbol: str, qty: Decimal | None = None) -> OrderResult | None:
        positions = {p.symbol: p for p in await self.get_positions()}
        if symbol not in positions:
            return None
        await self.cancel_all(symbol)  # защитные ордера держат объём — снять перед закрытием
        params = {"qty": str(qty)} if qty is not None else None
        order = await self._t.request("DELETE", f"/v2/positions/{symbol}", params=params)
        return _order_result(order) if order else None

    async def cancel_all(self, symbol: str | None = None) -> None:
        if symbol is None:
            await self._t.request("DELETE", "/v2/orders")
            return
        for o in await self._open_orders(symbol):
            await self._t.request("DELETE", f"/v2/orders/{o['id']}", allow_404=True)


def _order_result(o: dict[str, Any]) -> OrderResult:
    avg = _dec(o.get("filled_avg_price"))
    return OrderResult(
        order_id=str(o.get("id", "")),
        link_id=str(o.get("client_order_id", "")),
        status=str(o.get("status", "")),
        avg_price=avg or None,
        filled_qty=_dec(o.get("filled_qty")),
        raw=o,
    )
