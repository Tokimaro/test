import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.brokers.alpaca import AlpacaAdapter, AlpacaClient
from app.brokers.base import BrokerError, OrderRequest, OrderType
from app.config import BACKEND_DIR
from app.domain import Direction, MarketType, Timeframe
from app.market.sessions import SessionCalendar
from app.trading_config import TradingConfig

T = "https://trade.test"
D = "https://data.test"


def ms(y: int, mo: int, d: int, h: int, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------ сессии
def test_regular_session_with_dst() -> None:
    cal = SessionCalendar()
    # лето (EDT, UTC-4): сессия 13:30–20:00 UTC; вторник 2026-07-14
    assert not cal.is_open(ms(2026, 7, 14, 13, 29))
    assert cal.is_open(ms(2026, 7, 14, 13, 30))
    assert not cal.is_open(ms(2026, 7, 14, 20, 0))
    # зима (EST, UTC-5): 14:30–21:00 UTC; вторник 2026-01-13
    assert not cal.is_open(ms(2026, 1, 13, 14, 0))
    assert cal.is_open(ms(2026, 1, 13, 20, 30))
    # выходные
    assert not cal.is_open(ms(2026, 7, 18, 15, 0))


def test_buffer_and_calendar_holidays() -> None:
    cal = SessionCalendar()
    cal.load(
        [
            (date(2026, 7, 2), time(9, 30), time(16, 0)),
            (date(2026, 7, 6), time(9, 30), time(16, 0)),  # 3 июля — праздник: записи нет
            (date(2026, 11, 27), time(9, 30), time(13, 0)),  # сокращённый день
        ]
    )
    assert not cal.is_open(ms(2026, 7, 3, 15, 0))
    assert cal.is_open(ms(2026, 7, 2, 13, 44))
    assert not cal.is_open(ms(2026, 7, 2, 13, 44), buffer_minutes=15)
    assert cal.is_open(ms(2026, 7, 2, 13, 45), buffer_minutes=15)
    assert not cal.is_open(ms(2026, 7, 2, 19, 50), buffer_minutes=15)
    assert not cal.is_open(ms(2026, 11, 27, 18, 30))  # после 13:00 ET


# ------------------------------------------------------------------ Alpaca
def adapter(**kw: Any) -> AlpacaAdapter:
    return AlpacaAdapter(
        AlpacaClient(T, "KEY", "SECRET", backoff_s=0),
        AlpacaClient(D, "KEY", "SECRET", backoff_s=0),
        fill_timeout_s=2,
        **kw,
    )


def order(**kw: Any) -> dict[str, Any]:
    return {
        "id": "o1",
        "client_order_id": "c1",
        "status": "new",
        "symbol": "AAPL",
        "side": "sell",
        "qty": "10",
        "type": "limit",
        **kw,
    }


@respx.mock
async def test_auth_headers_and_instrument() -> None:
    route = respx.get(f"{T}/v2/assets/AAPL").mock(
        return_value=httpx.Response(200, json={"symbol": "AAPL", "tradable": True})
    )
    respx.get(f"{T}/v2/assets/NOPE").mock(return_value=httpx.Response(404, json={}))
    a = adapter()
    inst = await a.get_instrument("AAPL")
    assert inst.qty_step == 1 and inst.market_type is MarketType.STOCK
    h = route.calls.last.request.headers
    assert h["APCA-API-KEY-ID"] == "KEY" and h["APCA-API-SECRET-KEY"] == "SECRET"
    with pytest.raises(BrokerError):
        await a.get_instrument("NOPE")
    await a.aclose()


@respx.mock
async def test_bars_pagination_and_calendar() -> None:
    respx.get(f"{T}/v2/calendar").mock(
        return_value=httpx.Response(
            200, json=[{"date": "2026-07-14", "open": "09:30", "close": "16:00"}]
        )
    )
    pages = [
        {
            "bars": [{"t": "2026-07-14T13:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
            "next_page_token": "p2",
        },
        {
            "bars": [{"t": "2026-07-14T14:00:00Z", "o": 1.5, "h": 2, "l": 1, "c": 1.8, "v": 50}],
            "next_page_token": None,
        },
    ]
    route = respx.get(f"{D}/v2/stocks/AAPL/bars").mock(
        side_effect=[httpx.Response(200, json=p) for p in pages]
    )
    a = adapter(clock=lambda: ms(2026, 7, 14, 18))
    bars = await a.get_candles("AAPL", Timeframe.H1, ms(2026, 7, 14, 0), ms(2026, 7, 14, 18))
    assert [b.ts for b in bars] == [ms(2026, 7, 14, 13), ms(2026, 7, 14, 14)]
    assert route.calls[0].request.url.params["timeframe"] == "1Hour"
    assert route.calls[1].request.url.params["page_token"] == "p2"
    assert a.is_market_open("AAPL", ms(2026, 7, 14, 15))
    await a.aclose()


@respx.mock
async def test_balance_never_leveraged() -> None:
    respx.get(f"{T}/v2/account").mock(
        return_value=httpx.Response(200, json={"equity": "10000", "buying_power": "40000"})
    )
    bal = await adapter().get_balance()
    assert bal.equity == 10_000 and bal.available == 10_000


@respx.mock
async def test_positions_stop_must_cover_full_qty() -> None:
    respx.get(f"{T}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "side": "long",
                    "qty": "10",
                    "avg_entry_price": "200",
                    "unrealized_pl": "5",
                },
            ],
        )
    )
    orders = respx.get(f"{T}/v2/orders").mock(
        return_value=httpx.Response(
            200,
            json=[
                order(id="s1", type="stop", stop_price="195", qty="4"),
                order(id="l1", type="limit", limit_price="210", qty="4"),
            ],
        )
    )
    a = adapter()
    (p,) = await a.get_positions()
    assert p.stop_loss is None  # стоп на 4 из 10 акций — позиция не защищена
    orders.mock(
        return_value=httpx.Response(
            200,
            json=[
                order(id="s1", type="stop", stop_price="195", qty="4"),
                order(id="s2", type="stop", stop_price="195", qty="6"),
                order(id="l1", type="limit", limit_price="206", qty="4"),
                order(id="l2", type="limit", limit_price="212", qty="6"),
            ],
        )
    )
    (p,) = await a.get_positions()
    assert p.stop_loss == Decimal(195) and p.take_profit == Decimal(212)


@respx.mock
async def test_entry_waits_for_fill_then_protects_with_oco() -> None:
    posts = respx.post(f"{T}/v2/orders").mock(
        side_effect=[
            httpx.Response(
                200, json=order(id="e1", client_order_id="tb1-entry", side="buy", type="market")
            ),
            httpx.Response(200, json=order(id="oco1")),
        ]
    )
    respx.get(f"{T}/v2/orders/e1").mock(
        side_effect=[
            httpx.Response(200, json=order(id="e1", status="new")),
            httpx.Response(
                200,
                json=order(
                    id="e1",
                    client_order_id="tb1-entry",
                    status="filled",
                    filled_qty="10",
                    filled_avg_price="200.5",
                ),
            ),
        ]
    )
    a = adapter()
    res = await a.place_order(
        OrderRequest(
            "AAPL",
            Direction.LONG,
            Decimal(10),
            "tb1-entry",
            stop_loss=Decimal(195),
            take_profit=Decimal(212),
        )
    )
    assert res.status == "filled" and res.avg_price == Decimal("200.5")
    entry = json.loads(posts.calls[0].request.content)
    oco = json.loads(posts.calls[1].request.content)
    assert (
        entry["type"] == "market"
        and entry["side"] == "buy"
        and entry["client_order_id"] == "tb1-entry"
    )
    assert oco["order_class"] == "oco" and oco["side"] == "sell" and oco["qty"] == "10"
    assert oco["stop_loss"] == {"stop_price": "195"} and oco["take_profit"] == {
        "limit_price": "212"
    }
    assert oco["time_in_force"] == "gtc"


async def test_fractional_qty_rejected() -> None:
    with pytest.raises(BrokerError, match="целым"):
        await adapter().place_order(OrderRequest("AAPL", Direction.LONG, Decimal("1.5"), "x"))


async def test_separate_tp1_limit_rejected() -> None:
    a = adapter()
    with pytest.raises(BrokerError, match="partial_take_profit"):
        await a.place_order(
            OrderRequest(
                "AAPL",
                Direction.SHORT,
                Decimal(5),
                "tb1-tp1",
                order_type=OrderType.LIMIT,
                price=Decimal(207),
                reduce_only=True,
            )
        )


def _filled_entry() -> list[httpx.Response]:
    return [
        httpx.Response(
            200,
            json=order(
                id="e1",
                client_order_id="tb1-entry",
                status="filled",
                filled_qty="10",
                filled_avg_price="200",
            ),
        ),
    ]


@respx.mock
async def test_entry_with_partial_tp_places_two_oco_tranches() -> None:
    posts = respx.post(f"{T}/v2/orders").mock(return_value=httpx.Response(200, json=order(id="e1")))
    respx.get(f"{T}/v2/orders/e1").mock(side_effect=_filled_entry())
    await adapter().place_order(
        OrderRequest(
            "AAPL",
            Direction.LONG,
            Decimal(10),
            "tb1-entry",
            stop_loss=Decimal(195),
            take_profit=Decimal(212),
            partial_take_profit=(Decimal(207), Decimal(5)),
        )
    )
    bodies = [json.loads(c.request.content) for c in posts.calls[1:]]
    assert [(b["qty"], b["take_profit"]["limit_price"]) for b in bodies] == [
        ("5", "207"),
        ("5", "212"),
    ]
    assert all(b["stop_loss"]["stop_price"] == "195" for b in bodies)  # стоп на весь объём


@respx.mock
async def test_failed_protection_flattens_position() -> None:
    def post(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("order_class") == "oco":
            return httpx.Response(422, json={"code": 42210000, "message": "invalid stop"})
        return httpx.Response(200, json=order(id="e1"))

    posts = respx.post(f"{T}/v2/orders").mock(side_effect=post)
    respx.get(f"{T}/v2/orders/e1").mock(side_effect=_filled_entry())
    respx.get(f"{T}/v2/orders").mock(return_value=httpx.Response(200, json=[]))
    flatten = respx.delete(f"{T}/v2/positions/AAPL").mock(
        return_value=httpx.Response(200, json=order())
    )
    with pytest.raises(BrokerError, match="позиция закрыта"):
        await adapter().place_order(
            OrderRequest(
                "AAPL",
                Direction.LONG,
                Decimal(10),
                "tb1-entry",
                stop_loss=Decimal(195),
                take_profit=Decimal(212),
            )
        )
    assert flatten.called
    assert sum(1 for c in posts.calls if b"oco" in c.request.content) == 2  # повтор один раз


@respx.mock
async def test_fill_timeout_protects_partial_fill() -> None:
    posts = respx.post(f"{T}/v2/orders").mock(return_value=httpx.Response(200, json=order(id="e1")))
    respx.get(f"{T}/v2/orders/e1").mock(
        side_effect=[
            httpx.Response(200, json=order(id="e1", status="partially_filled", filled_qty="4")),
            httpx.Response(
                200, json=order(id="e1", status="canceled", filled_qty="4", filled_avg_price="200")
            ),
        ]
    )
    cancel = respx.delete(f"{T}/v2/orders/e1").mock(return_value=httpx.Response(204))
    a = AlpacaAdapter(
        AlpacaClient(T, "K", "S", backoff_s=0), AlpacaClient(D, "K", "S"), fill_timeout_s=0
    )
    res = await a.place_order(
        OrderRequest(
            "AAPL",
            Direction.LONG,
            Decimal(10),
            "tb1-entry",
            stop_loss=Decimal(195),
            take_profit=Decimal(212),
        )
    )
    assert cancel.called and res.filled_qty == 4
    assert json.loads(posts.calls.last.request.content)["qty"] == "4"


@respx.mock
async def test_amend_patches_stops_and_covers_gap_without_cancelling() -> None:
    respx.get(f"{T}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "side": "long", "qty": "10", "avg_entry_price": "200"},
            ],
        )
    )
    respx.get(f"{T}/v2/orders").mock(
        return_value=httpx.Response(
            200,
            json=[
                order(id="s1", type="stop", stop_price="195", qty="6"),
                order(id="l1", type="limit", limit_price="212", qty="6"),
            ],
        )
    )
    patches = respx.patch(f"{T}/v2/orders/s1").mock(return_value=httpx.Response(200, json={}))
    posts = respx.post(f"{T}/v2/orders").mock(return_value=httpx.Response(200, json=order()))
    deletes = respx.delete(url__regex=rf"{T}/v2/orders.*").mock(return_value=httpx.Response(204))
    await adapter().amend_stops("AAPL", stop_loss=Decimal("200.10"))
    assert json.loads(patches.calls.last.request.content) == {"stop_price": "200.10"}
    extra = json.loads(posts.calls.last.request.content)
    assert extra["type"] == "stop" and extra["qty"] == "4" and extra["stop_price"] == "200.10"
    assert not deletes.called  # защита ни на миг не снималась


@respx.mock
async def test_close_waits_for_cancellation_and_restores_stop_on_failure() -> None:
    respx.get(f"{T}/v2/positions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "AAPL", "side": "long", "qty": "10", "avg_entry_price": "200"},
            ],
        )
    )
    stop = order(id="s1", type="stop", stop_price="195", qty="10")
    respx.get(f"{T}/v2/orders").mock(
        side_effect=[
            httpx.Response(200, json=[stop]),  # get_positions
            httpx.Response(200, json=[stop]),  # cancel_all
            httpx.Response(200, json=[stop]),  # ещё pending_cancel
            httpx.Response(200, json=[]),  # отменён
        ]
    )
    respx.delete(f"{T}/v2/orders/s1").mock(return_value=httpx.Response(204))
    respx.delete(f"{T}/v2/positions/AAPL").mock(
        return_value=httpx.Response(403, json={"message": "halted"})
    )
    restore = respx.post(f"{T}/v2/orders").mock(return_value=httpx.Response(200, json=order()))
    with pytest.raises(BrokerError):
        await adapter().close_position("AAPL")
    body = json.loads(restore.calls.last.request.content)
    assert body["type"] == "stop" and body["stop_price"] == "195" and body["qty"] == "10"


@respx.mock
async def test_closed_pnl_paginates() -> None:
    page1 = [
        {
            "id": f"a{i}",
            "symbol": "MSFT",
            "side": "buy",
            "qty": "1",
            "price": "1",
            "transaction_time": "2026-07-14T14:00:00Z",
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": "b1",
            "symbol": "AAPL",
            "side": "buy",
            "qty": "2",
            "price": "100",
            "transaction_time": "2026-07-14T15:00:00Z",
        },
        {
            "id": "b2",
            "symbol": "AAPL",
            "side": "sell",
            "qty": "2",
            "price": "90",
            "transaction_time": "2026-07-14T16:00:00Z",
        },
    ]
    route = respx.get(f"{T}/v2/account/activities/FILL").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    (c,) = await adapter().get_closed_pnl("AAPL", 0)
    assert c.pnl == Decimal(-20)
    assert route.calls.last.request.url.params["page_token"] == "a99"


@respx.mock
async def test_closed_pnl_from_fills() -> None:
    respx.get(f"{T}/v2/account/activities/FILL").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "6",
                    "price": "100",
                    "transaction_time": "2026-07-14T14:00:00Z",
                },
                {
                    "symbol": "AAPL",
                    "side": "buy",
                    "qty": "4",
                    "price": "105",
                    "transaction_time": "2026-07-14T14:01:00Z",
                },
                {
                    "symbol": "MSFT",
                    "side": "buy",
                    "qty": "1",
                    "price": "400",
                    "transaction_time": "2026-07-14T14:02:00Z",
                },
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "qty": "5",
                    "price": "110",
                    "transaction_time": "2026-07-14T15:00:00Z",
                },
                {
                    "symbol": "AAPL",
                    "side": "sell",
                    "qty": "5",
                    "price": "99",
                    "transaction_time": "2026-07-14T16:00:00Z",
                },
            ],
        )
    )
    closes = await adapter().get_closed_pnl("AAPL", 0)
    assert [c.qty for c in closes] == [5, 5]
    assert closes[0].avg_entry == Decimal(102)  # (6×100 + 4×105) / 10
    assert closes[0].pnl == Decimal(40) and closes[1].pnl == Decimal(-15)


@respx.mock
async def test_get_order_404_is_none_and_retries_on_429() -> None:
    respx.get(f"{T}/v2/orders:by_client_order_id").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(404, json={"message": "not found"}),
        ]
    )
    assert await adapter().get_order("AAPL", "tb9-entry") is None


def test_stocks_market_is_off_and_cannot_be_enabled() -> None:
    """Стратегия на акциях не проверялась: рынок выключен, включить его конфигурация не даст."""
    raw = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml").model_dump(mode="json")
    stocks = raw["markets"]["stocks"]
    assert stocks["broker"] == "alpaca" and not stocks["enabled"]
    stocks["enabled"] = True
    with pytest.raises(ValueError, match="не проверялся"):
        TradingConfig.from_dict(raw)
