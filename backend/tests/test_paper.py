from decimal import Decimal

import pytest

from app.brokers.base import BrokerError, OrderRequest, OrderType
from app.brokers.paper import DUPLICATE_LINK_ID, PaperBroker
from app.domain import Candle, Direction
from tests.fakes import FakeMarketBroker

D = Decimal


def candle(o: float, h: float, lo: float, c: float, ts: int = 0) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=1)


async def broker() -> PaperBroker:
    b = PaperBroker(FakeMarketBroker(), initial_equity=D(10_000), slippage_pct=D(0))
    await b.on_candle("BTCUSDT", candle(100, 100, 100, 100))
    return b


def order(
    link: str, direction: Direction = Direction.LONG, qty: str = "10", **kw: object
) -> OrderRequest:
    return OrderRequest(symbol="BTCUSDT", direction=direction, qty=D(qty), link_id=link, **kw)  # type: ignore[arg-type]


async def test_stop_loss_closes_and_accounts_fees() -> None:
    b = await broker()
    res = await b.place_order(order("e1", stop_loss=D(97), take_profit=D(109)))
    assert res.status == "Filled" and res.avg_price == D(100)
    await b.on_candle("BTCUSDT", candle(100, 101, 96, 97, ts=1))
    assert b.positions == {}
    (closed,) = b.closed
    fee = D("0.00055")
    expected = (D(97) - D(100)) * 10 - D(97) * 10 * fee - D(100) * 10 * fee
    assert closed.pnl == pytest.approx(expected)
    assert b.cash == pytest.approx(D(10_000) + expected)


async def test_stop_wins_when_both_touched() -> None:
    b = await broker()
    await b.place_order(order("e1", stop_loss=D(97), take_profit=D(103)))
    await b.on_candle("BTCUSDT", candle(100, 104, 96, 100, ts=1))
    assert b.closed[0].avg_exit == D(97)


async def test_gap_through_stop_fills_at_open() -> None:
    b = await broker()
    await b.place_order(order("e1", direction=Direction.SHORT, stop_loss=D(103)))
    await b.on_candle("BTCUSDT", candle(105, 106, 104, 105, ts=1))
    assert b.closed[0].avg_exit == D(105)


async def test_tp1_limit_then_take_profit() -> None:
    b = await broker()
    await b.place_order(order("e1", stop_loss=D(97), take_profit=D(109)))
    await b.place_order(
        order(
            "tp1",
            direction=Direction.SHORT,
            qty="5",
            order_type=OrderType.LIMIT,
            price=D("104.5"),
            reduce_only=True,
        )
    )
    await b.on_candle("BTCUSDT", candle(100, 105, 99.5, 104.8, ts=1))
    assert b.positions["BTCUSDT"].qty == D(5)
    assert (await b.get_order("BTCUSDT", "tp1")).status == "Filled"  # type: ignore[union-attr]
    # TP1 исполнен лимиткой — maker-комиссия
    assert b.closed[0].avg_exit == D("104.5")
    await b.on_candle("BTCUSDT", candle(105, 110, 104, 109.5, ts=2))
    assert b.positions == {}
    assert sum(c.qty for c in b.closed) == D(10)
    total = sum(c.pnl for c in b.closed)
    assert b.cash == pytest.approx(D(10_000) + total)


async def test_limits_cancelled_when_position_stopped_out() -> None:
    b = await broker()
    await b.place_order(order("e1", stop_loss=D(97)))
    await b.place_order(
        order(
            "tp1",
            direction=Direction.SHORT,
            qty="5",
            order_type=OrderType.LIMIT,
            price=D(104),
            reduce_only=True,
        )
    )
    await b.on_candle("BTCUSDT", candle(100, 100, 96, 96, ts=1))
    assert b.limits == {}
    assert (await b.get_order("BTCUSDT", "tp1")).status == "Cancelled"  # type: ignore[union-attr]


async def test_duplicate_link_id_like_bybit() -> None:
    b = await broker()
    await b.place_order(order("dup"))
    with pytest.raises(BrokerError) as exc:
        await b.place_order(order("dup"))
    assert exc.value.code == DUPLICATE_LINK_ID


async def test_balance_and_partial_close() -> None:
    b = await broker()
    await b.set_leverage("BTCUSDT", D(5))
    await b.place_order(order("e1"))
    await b.on_candle("BTCUSDT", candle(100, 111, 100, 110, ts=1))
    bal = await b.get_balance()
    fee = D(100) * 10 * D("0.00055")
    assert bal.equity == pytest.approx(D(10_000) - fee + D(100))
    assert bal.available == pytest.approx(bal.equity - D(200))  # 1000 notional / 5x
    await b.close_position("BTCUSDT", D(4))
    assert b.positions["BTCUSDT"].qty == D(6)
    assert await b.close_position("ETHUSDT") is None


async def test_state_roundtrip() -> None:
    b = await broker()
    await b.place_order(order("e1", stop_loss=D(97), take_profit=D(109)))
    await b.place_order(
        order(
            "tp1",
            direction=Direction.SHORT,
            qty="5",
            order_type=OrderType.LIMIT,
            price=D(104),
            reduce_only=True,
        )
    )
    restored = PaperBroker(FakeMarketBroker())
    restored.load_dict(b.to_dict())
    assert restored.cash == b.cash
    assert restored.positions == b.positions
    assert restored.limits == b.limits
    with pytest.raises(BrokerError):
        await restored.place_order(order("e1"))
