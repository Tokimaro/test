"""Адаптер Bybit V5 (Unified Trading Account, one-way режим позиций)."""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

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
from app.brokers.bybit.client import BybitHttpClient
from app.brokers.bybit.ws import BybitStream, Connector, _default_connect, public_url
from app.domain import Balance, Candle, Direction, Instrument, MarketType, Position, Timeframe

log = structlog.get_logger()

KLINE_LIMIT = 1000
LEVERAGE_NOT_MODIFIED = 110043
DERIVATIVE_CATEGORIES = {"linear", "inverse"}


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal(default)


def _side(direction: Direction) -> str:
    return "Buy" if direction is Direction.LONG else "Sell"


def parse_kline_row(row: list[str]) -> Candle:
    return Candle(
        ts=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        turnover=float(row[6]),
    )


def parse_ws_kline(topic: str, item: dict[str, Any]) -> CandleClosed | None:
    """topic = kline.{interval}.{symbol}. Возвращает событие только для закрытой свечи."""
    if not item.get("confirm"):
        return None
    _, interval, symbol = topic.split(".", 2)
    candle = Candle(
        ts=int(item["start"]),
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=float(item["volume"]),
        turnover=float(item.get("turnover", 0)),
    )
    return CandleClosed(symbol=symbol, timeframe=Timeframe(interval), candle=candle)


class BybitAdapter(BrokerAdapter):
    name = "bybit"

    def __init__(
        self,
        client: BybitHttpClient,
        *,
        category: str = "linear",
        market_type: MarketType = MarketType.CRYPTO,
        testnet: bool = True,
        settle_coin: str = "USDT",
        ws_connect: Connector = _default_connect,
        ws_url: str | None = None,
        ws_options: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._category = category
        self._market_type = market_type
        self._testnet = testnet
        self._settle_coin = settle_coin
        self._ws_connect = ws_connect
        self._ws_url = ws_url or public_url(category, testnet)
        self._ws_options = ws_options or {}
        self._instruments: dict[str, Instrument] = {}

    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def category(self) -> str:
        return self._category

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ market data
    async def server_time_ms(self) -> int:
        return await self._client.sync_time()

    async def get_instrument(self, symbol: str) -> Instrument:
        if symbol in self._instruments:
            return self._instruments[symbol]
        result = await self._client.request(
            "GET", "/v5/market/instruments-info", {"category": self._category, "symbol": symbol}
        )
        items = result.get("list") or []
        if not items:
            raise BrokerError(f"инструмент {symbol} не найден в категории {self._category}")
        info = items[0]
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})
        is_spot = self._category == "spot"
        taker, maker = await self._fee_rates(symbol)
        inst = Instrument(
            symbol=symbol,
            market_type=self._market_type,
            category=self._category,
            tick_size=_dec(price.get("tickSize")),
            qty_step=_dec(lot.get("basePrecision") if is_spot else lot.get("qtyStep")),
            min_qty=_dec(lot.get("minOrderQty")),
            max_qty=_dec(lot.get("maxOrderQty")),
            min_notional=_dec(lot.get("minOrderAmt") if is_spot else lot.get("minNotionalValue")),
            max_leverage=_dec(lev.get("maxLeverage"), "1"),
            taker_fee=taker,
            maker_fee=maker,
        )
        if inst.tick_size <= 0 or inst.qty_step <= 0:
            raise BrokerError(f"некорректные фильтры инструмента {symbol}: {info}")
        self._instruments[symbol] = inst
        return inst

    async def _fee_rates(self, symbol: str) -> tuple[Decimal, Decimal]:
        default = (Decimal("0.00055"), Decimal("0.0002"))
        if not self._client.has_credentials:
            return default
        try:
            result = await self._client.request(
                "GET",
                "/v5/account/fee-rate",
                {"category": self._category, "symbol": symbol},
                auth=True,
            )
        except BrokerError as exc:
            log.warning("bybit.fee_rate_unavailable", symbol=symbol, error=str(exc))
            return default
        items = result.get("list") or []
        if not items:
            return default
        return _dec(items[0].get("takerFeeRate")), _dec(items[0].get("makerFeeRate"))

    async def get_funding_rate(self, symbol: str) -> float | None:
        if self._category not in DERIVATIVE_CATEGORIES:
            return None
        result = await self._client.request(
            "GET", "/v5/market/tickers", {"category": self._category, "symbol": symbol}
        )
        items = result.get("list") or []
        rate = items[0].get("fundingRate") if items else None
        return float(rate) if rate not in (None, "") else None

    async def get_candles(
        self, symbol: str, timeframe: Timeframe, start_ms: int, end_ms: int
    ) -> list[Candle]:
        """Постранично загружает свечи окнами по KLINE_LIMIT штук."""
        candles: dict[int, Candle] = {}
        window = KLINE_LIMIT * timeframe.ms
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(cursor + window - 1, end_ms)
            result = await self._client.request(
                "GET",
                "/v5/market/kline",
                {
                    "category": self._category,
                    "symbol": symbol,
                    "interval": timeframe.value,
                    "start": cursor,
                    "end": window_end,
                    "limit": KLINE_LIMIT,
                },
            )
            for row in result.get("list") or []:
                c = parse_kline_row(row)
                if start_ms <= c.ts <= end_ms:
                    candles[c.ts] = c
            cursor = window_end + 1
        return [candles[ts] for ts in sorted(candles)]

    async def stream_candles(
        self, subscriptions: list[tuple[str, Timeframe]]
    ) -> AsyncGenerator[StreamEvent]:
        topics = [f"kline.{tf.value}.{symbol}" for symbol, tf in subscriptions]
        stream = BybitStream(self._ws_url, topics, connect=self._ws_connect, **self._ws_options)
        async for msg in stream.messages():
            if isinstance(msg, StreamReconnected):
                yield msg
                continue
            topic = str(msg.get("topic", ""))
            if not topic.startswith("kline."):
                continue
            for item in msg.get("data") or []:
                event = parse_ws_kline(topic, item)
                if event is not None:
                    yield event

    # ------------------------------------------------------------------ account / trading
    def _require_derivatives(self) -> None:
        if self._category not in DERIVATIVE_CATEGORIES:
            raise BrokerError(
                f"торговля в категории {self._category} пока не поддерживается адаптером"
            )

    async def get_balance(self) -> Balance:
        result = await self._client.request(
            "GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, auth=True
        )
        accounts = result.get("list") or []
        if not accounts:
            raise BrokerError("пустой ответ wallet-balance")
        acc = accounts[0]
        return Balance(
            equity=_dec(acc.get("totalEquity")),
            available=_dec(acc.get("totalAvailableBalance")),
            currency="USD",
        )

    async def get_positions(self) -> list[Position]:
        self._require_derivatives()
        result = await self._client.request(
            "GET",
            "/v5/position/list",
            {"category": self._category, "settleCoin": self._settle_coin, "limit": 200},
            auth=True,
        )
        positions = []
        for p in result.get("list") or []:
            size = _dec(p.get("size"))
            if size == 0 or p.get("side") not in ("Buy", "Sell"):
                continue
            sl, tp = _dec(p.get("stopLoss")), _dec(p.get("takeProfit"))
            liq = _dec(p.get("liqPrice"))
            positions.append(
                Position(
                    symbol=p["symbol"],
                    direction=Direction.LONG if p["side"] == "Buy" else Direction.SHORT,
                    qty=size,
                    entry_price=_dec(p.get("avgPrice")),
                    stop_loss=sl or None,
                    take_profit=tp or None,
                    unrealized_pnl=_dec(p.get("unrealisedPnl")),
                    leverage=_dec(p.get("leverage"), "1"),
                    liq_price=liq or None,
                )
            )
        return positions

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        self._require_derivatives()
        lev = str(leverage.normalize())
        try:
            await self._client.request(
                "POST",
                "/v5/position/set-leverage",
                {
                    "category": self._category,
                    "symbol": symbol,
                    "buyLeverage": lev,
                    "sellLeverage": lev,
                },
                auth=True,
            )
        except BrokerError as exc:
            if exc.code != LEVERAGE_NOT_MODIFIED:
                raise

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self._require_derivatives()
        if req.qty <= 0:
            raise BrokerError("qty должен быть > 0")
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": req.symbol,
            "side": _side(req.direction),
            "orderType": req.order_type.value,
            "qty": str(req.qty),
            "orderLinkId": req.link_id,
            "positionIdx": 0,
            "reduceOnly": req.reduce_only,
        }
        if req.order_type is OrderType.LIMIT:
            if req.price is None:
                raise BrokerError("для лимитного ордера нужна цена")
            body["price"] = str(req.price)
            body["timeInForce"] = "GTC"
        if req.stop_loss is not None or req.take_profit is not None:
            body["tpslMode"] = "Full"
            if req.stop_loss is not None:
                body["stopLoss"] = str(req.stop_loss)
                body["slTriggerBy"] = "MarkPrice"
            if req.take_profit is not None:
                body["takeProfit"] = str(req.take_profit)
                body["tpTriggerBy"] = "LastPrice"
        result = await self._client.request("POST", "/v5/order/create", body, auth=True)
        return OrderResult(
            order_id=str(result.get("orderId", "")),
            link_id=str(result.get("orderLinkId", req.link_id)),
            raw=result,
        )

    async def get_order(self, symbol: str, link_id: str) -> OrderResult | None:
        """Ищет ордер по клиентскому id среди активных, затем в истории."""
        params = {"category": self._category, "symbol": symbol, "orderLinkId": link_id}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            result = await self._client.request("GET", path, params, auth=True)
            items = result.get("list") or []
            if items:
                o: dict[str, Any] = items[0]
                avg = _dec(o.get("avgPrice"))
                return OrderResult(
                    order_id=str(o.get("orderId", "")),
                    link_id=str(o.get("orderLinkId", link_id)),
                    status=str(o.get("orderStatus", "")),
                    avg_price=avg or None,
                    filled_qty=_dec(o.get("cumExecQty")),
                    raw=o,
                )
        return None

    async def get_closed_pnl(self, symbol: str, since_ms: int) -> list[ClosedPnl]:
        self._require_derivatives()
        result = await self._client.request(
            "GET",
            "/v5/position/closed-pnl",
            {"category": self._category, "symbol": symbol, "startTime": since_ms, "limit": 100},
            auth=True,
        )
        items = [
            ClosedPnl(
                symbol=str(r.get("symbol", symbol)),
                qty=_dec(r.get("closedSize") or r.get("qty")),
                avg_entry=_dec(r.get("avgEntryPrice")),
                avg_exit=_dec(r.get("avgExitPrice")),
                pnl=_dec(r.get("closedPnl")),
                ts=int(r.get("updatedTime") or r.get("createdTime") or 0),
                order_id=str(r.get("orderId", "")),
            )
            for r in result.get("list") or []
        ]
        return sorted(items, key=lambda c: c.ts)

    async def amend_stops(
        self,
        symbol: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> None:
        self._require_derivatives()
        if stop_loss is None and take_profit is None:
            return
        body: dict[str, Any] = {
            "category": self._category,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
        }
        if stop_loss is not None:
            body["stopLoss"] = str(stop_loss)
            body["slTriggerBy"] = "MarkPrice"
        if take_profit is not None:
            body["takeProfit"] = str(take_profit)
            body["tpTriggerBy"] = "LastPrice"
        await self._client.request("POST", "/v5/position/trading-stop", body, auth=True)

    async def close_position(self, symbol: str, qty: Decimal | None = None) -> OrderResult | None:
        self._require_derivatives()
        position = next((p for p in await self.get_positions() if p.symbol == symbol), None)
        if position is None:
            return None
        close_qty = position.qty if qty is None else min(qty, position.qty)
        return await self.place_order(
            OrderRequest(
                symbol=symbol,
                direction=position.direction.opposite,
                qty=close_qty,
                link_id=f"close-{uuid.uuid4().hex[:20]}",
                reduce_only=True,
            )
        )

    async def cancel_all(self, symbol: str | None = None) -> None:
        body: dict[str, Any] = {"category": self._category}
        if symbol is not None:
            body["symbol"] = symbol
        else:
            body["settleCoin"] = self._settle_coin
        await self._client.request("POST", "/v5/order/cancel-all", body, auth=True)
