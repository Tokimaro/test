import json
from decimal import Decimal

import httpx
import pytest
import respx

from app.brokers.base import BrokerError, OrderRequest, OrderType
from app.brokers.bybit.adapter import BybitAdapter, parse_ws_kline
from app.brokers.bybit.client import BybitHttpClient
from app.domain import Direction, Timeframe

BASE = "https://bybit.test"
H = Timeframe.H1.ms


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def adapter(category: str = "linear", keys: bool = True) -> BybitAdapter:
    client = BybitHttpClient(
        base_url=BASE,
        api_key="K" if keys else "",
        api_secret="S" if keys else "",
        backoff_base_s=0,
    )
    return BybitAdapter(client, category=category)


LINEAR_INFO = {
    "list": [
        {
            "symbol": "BTCUSDT",
            "priceFilter": {"tickSize": "0.10"},
            "lotSizeFilter": {
                "qtyStep": "0.001",
                "minOrderQty": "0.001",
                "maxOrderQty": "1190.000",
                "minNotionalValue": "5",
            },
            "leverageFilter": {"maxLeverage": "100.00"},
        }
    ]
}


@respx.mock
async def test_instrument_linear_with_fees() -> None:
    respx.get(f"{BASE}/v5/market/instruments-info").mock(return_value=ok(LINEAR_INFO))
    respx.get(f"{BASE}/v5/account/fee-rate").mock(
        return_value=ok({"list": [{"takerFeeRate": "0.0006", "makerFeeRate": "0.0001"}]})
    )
    a = adapter()
    inst = await a.get_instrument("BTCUSDT")
    assert inst.tick_size == Decimal("0.10")
    assert inst.qty_step == Decimal("0.001")
    assert inst.min_notional == Decimal(5)
    assert inst.max_leverage == Decimal(100)
    assert inst.taker_fee == Decimal("0.0006")
    # кэш: второй вызов не ходит в сеть
    await a.get_instrument("BTCUSDT")
    assert respx.calls.call_count == 2
    await a.aclose()


@respx.mock
async def test_instrument_spot_uses_base_precision_and_default_fees() -> None:
    respx.get(f"{BASE}/v5/market/instruments-info").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "symbol": "AAPLXUSDT",
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {
                            "basePrecision": "0.0001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "1000",
                            "minOrderAmt": "1",
                        },
                    }
                ]
            }
        )
    )
    a = adapter("spot", keys=False)
    inst = await a.get_instrument("AAPLXUSDT")
    assert inst.qty_step == Decimal("0.0001")
    assert inst.min_notional == Decimal(1)
    assert inst.max_leverage == Decimal(1)
    assert inst.taker_fee == Decimal("0.001")  # базовая ставка спота Bybit
    assert inst.maker_fee == Decimal("0.001")
    await a.aclose()


@respx.mock
async def test_unknown_instrument() -> None:
    respx.get(f"{BASE}/v5/market/instruments-info").mock(return_value=ok({"list": []}))
    a = adapter(keys=False)
    with pytest.raises(BrokerError):
        await a.get_instrument("NOPE")
    await a.aclose()


def kline_row(ts: int, close: float = 1.0) -> list[str]:
    return [str(ts), "1", "2", "0.5", str(close), "10", "100"]


@respx.mock
async def test_candles_paginate_sort_and_filter() -> None:
    start = 1_700_000_000_000 // H * H
    end = start + 1500 * H

    def handler(request: httpx.Request) -> httpx.Response:
        s = int(request.url.params["start"])
        e = int(request.url.params["end"])
        assert e - s < 1000 * H  # окно не больше лимита
        rows = [kline_row(ts) for ts in range(s, e + 1, H)]
        rows.append(kline_row(s - H))  # лишняя свеча за пределами запроса
        return ok({"list": list(reversed(rows))})  # Bybit отдаёт от новых к старым

    route = respx.get(f"{BASE}/v5/market/kline").mock(side_effect=handler)
    a = adapter(keys=False)
    candles = await a.get_candles("BTCUSDT", Timeframe.H1, start, end)
    assert route.call_count == 2
    ts = [c.ts for c in candles]
    assert ts == list(range(start, end + 1, H))
    await a.aclose()


def test_parse_ws_kline_only_confirmed() -> None:
    item = {
        "start": 1_700_000_000_000,
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
        "volume": "3",
        "turnover": "4",
        "confirm": False,
    }
    assert parse_ws_kline("kline.60.BTCUSDT", item) is None
    event = parse_ws_kline("kline.60.BTCUSDT", {**item, "confirm": True})
    assert event is not None
    assert event.symbol == "BTCUSDT"
    assert event.timeframe is Timeframe.H1
    assert event.candle.close == 1.5


@respx.mock
async def test_positions_parsing_skips_empty() -> None:
    respx.get(f"{BASE}/v5/position/list").mock(
        return_value=ok(
            {
                "list": [
                    {"symbol": "ETHUSDT", "side": "", "size": "0"},
                    {
                        "symbol": "BTCUSDT",
                        "side": "Sell",
                        "size": "0.5",
                        "avgPrice": "60000",
                        "stopLoss": "62000",
                        "takeProfit": "",
                        "unrealisedPnl": "-12.5",
                        "leverage": "5",
                        "liqPrice": "",
                    },
                ]
            }
        )
    )
    a = adapter()
    positions = await a.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.direction is Direction.SHORT
    assert p.qty == Decimal("0.5")
    assert p.stop_loss == Decimal(62000)
    assert p.take_profit is None
    assert p.liq_price is None
    await a.aclose()


@respx.mock
async def test_place_order_body() -> None:
    route = respx.post(f"{BASE}/v5/order/create").mock(
        return_value=ok({"orderId": "abc", "orderLinkId": "L1"})
    )
    a = adapter()
    res = await a.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            direction=Direction.LONG,
            qty=Decimal("0.010"),
            link_id="L1",
            stop_loss=Decimal("59000.0"),
            take_profit=Decimal("63000.0"),
        )
    )
    body = json.loads(route.calls.last.request.content)
    assert body["side"] == "Buy"
    assert body["orderType"] == "Market"
    assert body["qty"] == "0.010"
    assert body["orderLinkId"] == "L1"
    assert body["stopLoss"] == "59000.0"
    assert body["takeProfit"] == "63000.0"
    assert body["tpslMode"] == "Full"
    assert body["positionIdx"] == 0
    assert res.order_id == "abc"
    await a.aclose()


async def test_limit_order_requires_price() -> None:
    a = adapter()
    with pytest.raises(BrokerError):
        await a.place_order(
            OrderRequest(
                symbol="BTCUSDT",
                direction=Direction.SHORT,
                qty=Decimal(1),
                link_id="x",
                order_type=OrderType.LIMIT,
            )
        )
    await a.aclose()


@respx.mock
async def test_spot_market_order_in_base_coin() -> None:
    route = respx.post(f"{BASE}/v5/order/create").mock(
        return_value=ok({"orderId": "1", "orderLinkId": "tb1-b"})
    )
    a = adapter("spot")
    res = await a.place_order(
        OrderRequest(
            symbol="ETHUSDT", direction=Direction.LONG, qty=Decimal("0.5"), link_id="tb1-b"
        )
    )
    body = json.loads(route.calls[0].request.content)
    assert body["category"] == "spot" and body["side"] == "Buy"
    assert body["marketUnit"] == "baseCoin"  # иначе qty покупки считался бы в USDT
    assert "reduceOnly" not in body and "positionIdx" not in body
    assert res.order_id == "1"
    with pytest.raises(BrokerError, match="спот"):
        await a.place_order(
            OrderRequest(
                symbol="ETHUSDT",
                direction=Direction.LONG,
                qty=Decimal(1),
                link_id="x",
                stop_loss=Decimal(1),
            )
        )
    await a.aclose()


WALLET = {
    "list": [
        {
            "totalEquity": "12000",
            "totalAvailableBalance": "11000",
            "coin": [
                {"coin": "USDT", "walletBalance": "5000", "locked": "100"},
                {"coin": "ETH", "walletBalance": "1.2345678"},
                {"coin": "BTC", "walletBalance": "0"},
            ],
        }
    ]
}


@respx.mock
async def test_spot_holdings_and_cash() -> None:
    respx.get(f"{BASE}/v5/account/wallet-balance").mock(return_value=ok(WALLET))
    a = adapter("spot")
    (eth,) = await a.get_positions()
    assert eth.symbol == "ETHUSDT" and eth.qty == Decimal("1.2345678")
    assert eth.direction is Direction.LONG
    bal = await a.get_balance()
    assert bal.equity == Decimal(12000)
    assert bal.available == Decimal(4900)  # только свободные USDT, а не весь залог счёта
    assert await a.get_closed_pnl("ETHUSDT", 0) == []
    await a.aclose()


@respx.mock
async def test_spot_close_rounds_down_to_qty_step() -> None:
    respx.get(f"{BASE}/v5/account/wallet-balance").mock(return_value=ok(WALLET))
    respx.get(f"{BASE}/v5/market/instruments-info").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "symbol": "ETHUSDT",
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {
                            "basePrecision": "0.0001",
                            "minOrderQty": "0.0001",
                            "maxOrderQty": "1000",
                            "minOrderAmt": "1",
                        },
                    }
                ]
            }
        )
    )
    respx.get(f"{BASE}/v5/account/fee-rate").mock(
        return_value=ok({"list": [{"takerFeeRate": "0.001", "makerFeeRate": "0.001"}]})
    )
    route = respx.post(f"{BASE}/v5/order/create").mock(return_value=ok({"orderId": "2"}))
    a = adapter("spot")
    await a.close_position("ETHUSDT")
    body = json.loads(route.calls[0].request.content)
    assert body["side"] == "Sell" and body["qty"] == "1.2345"
    await a.aclose()


@respx.mock
async def test_cancel_order_missing_is_false() -> None:
    respx.post(f"{BASE}/v5/order/cancel").mock(
        return_value=httpx.Response(200, json={"retCode": 110001, "retMsg": "order not exists"})
    )
    a = adapter()
    assert await a.cancel_order("BTCUSDT", "tb1-entry") is False
    await a.aclose()


@respx.mock
async def test_set_leverage_not_modified_is_ok() -> None:
    respx.post(f"{BASE}/v5/position/set-leverage").mock(
        return_value=httpx.Response(200, json={"retCode": 110043, "retMsg": "not modified"})
    )
    a = adapter()
    await a.set_leverage("BTCUSDT", Decimal("5"))
    await a.aclose()


@respx.mock
async def test_close_position_partial_reduce_only() -> None:
    respx.get(f"{BASE}/v5/position/list").mock(
        return_value=ok(
            {"list": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.4", "avgPrice": "1"}]}
        )
    )
    route = respx.post(f"{BASE}/v5/order/create").mock(return_value=ok({"orderId": "c"}))
    a = adapter()
    await a.close_position("BTCUSDT", Decimal("1.0"))  # больше позиции — обрезается
    body = json.loads(route.calls.last.request.content)
    assert body["side"] == "Sell"
    assert body["reduceOnly"] is True
    assert body["qty"] == "0.4"
    assert await a.close_position("ETHUSDT") is None
    await a.aclose()


@respx.mock
async def test_get_order_falls_back_to_history() -> None:
    respx.get(f"{BASE}/v5/order/realtime").mock(return_value=ok({"list": []}))
    respx.get(f"{BASE}/v5/order/history").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "orderId": "o1",
                        "orderLinkId": "L1",
                        "orderStatus": "Filled",
                        "avgPrice": "100.5",
                        "cumExecQty": "2",
                    }
                ]
            }
        )
    )
    a = adapter()
    o = await a.get_order("BTCUSDT", "L1")
    assert o is not None
    assert o.status == "Filled"
    assert o.avg_price == Decimal("100.5")
    assert o.filled_qty == Decimal(2)
    await a.aclose()


@respx.mock
async def test_closed_pnl_sorted() -> None:
    respx.get(f"{BASE}/v5/position/closed-pnl").mock(
        return_value=ok(
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "closedSize": "1",
                        "avgEntryPrice": "100",
                        "avgExitPrice": "110",
                        "closedPnl": "9.8",
                        "updatedTime": "2000",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "closedSize": "1",
                        "avgEntryPrice": "100",
                        "avgExitPrice": "105",
                        "closedPnl": "4.9",
                        "updatedTime": "1000",
                    },
                ]
            }
        )
    )
    a = adapter()
    items = await a.get_closed_pnl("BTCUSDT", 0)
    assert [i.ts for i in items] == [1000, 2000]
    assert items[1].pnl == Decimal("9.8")
    await a.aclose()
