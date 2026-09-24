import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers.base import CandleClosed
from app.brokers.paper import PaperBroker
from app.config import BACKEND_DIR, RunMode, Settings
from app.core.events import EventBus
from app.core.runner import BotRuntime
from app.db.models import CandleRow, SignalRow, TradeRow
from app.domain import Timeframe
from app.market.feed import wall_clock_ms
from app.trading_config import TradingConfig
from tests.fakes import FakeMarketBroker
from tests.synthetic import frame_to_candles, make_ohlcv

pytestmark = pytest.mark.db


def setup_runtime(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> tuple[BotRuntime, EventBus]:
    raw = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml").model_dump(mode="json")
    raw["markets"] = {"crypto": {**raw["markets"]["crypto"], "symbols": ["BTCUSDT"]}}
    config = TradingConfig.from_dict(raw)

    day = Timeframe.D1.ms
    start = (wall_clock_ms() // day - 300) * day
    daily = make_ohlcv(300, drift=0.004, vol=0.02, seed=1, start_ts=start, tf=Timeframe.D1)
    candles = frame_to_candles(daily)
    fake = FakeMarketBroker({("BTCUSDT", Timeframe.D1): candles}, spot=True)
    fake.events = [CandleClosed("BTCUSDT", Timeframe.D1, candles[-1])]
    paper = PaperBroker(fake, initial_equity=Decimal(5_000))
    monkeypatch.setattr(BotRuntime, "_make_broker", lambda self, market: (paper, fake))

    settings = Settings(_env_file=None, database_url=migrated_db, mode=RunMode.PAPER)
    bus = EventBus()
    return BotRuntime(settings, config, bus), bus


async def test_runtime_start_feed_and_stop(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, bus = setup_runtime(migrated_db, monkeypatch)
    q = bus.subscribe()
    await runtime.start()
    await asyncio.sleep(0.5)  # фоновые задачи: поток свечей и сверка
    await runtime.stop()

    async with db_sessionmaker() as s:
        candles = await s.scalar(select(func.count()).select_from(CandleRow))
        signals = await s.scalar(select(func.count()).select_from(SignalRow))
        trades = list((await s.scalars(select(TradeRow))).all())
    assert candles is not None and candles >= 299
    assert signals == 1
    # первый расчёт сразу ребалансирует: BTC в растущем тренде — куплен
    assert len(trades) == 1 and trades[0].status == "open"
    types = set()
    while not q.empty():
        types.add(q.get_nowait().type)
    assert {"bot_status", "equity", "signal", "trade_opened", "rebalance"} <= types
    repo_state = await runtime.repo.get_state("paper_broker:crypto")
    assert repo_state is not None and Decimal(repo_state["cash"]) < Decimal(5_000)


async def test_halted_state_is_loaded_before_first_candle(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия: свежая свеча при старте не должна обходить сохранённый kill switch."""
    runtime, _ = setup_runtime(migrated_db, monkeypatch)
    await runtime.repo.set_state("risk", {"halted": True, "halt_reason": "kill_switch"})
    await runtime.start()
    await asyncio.sleep(0.3)
    await runtime.stop()
    async with db_sessionmaker() as s:
        sig = await s.scalar(select(SignalRow))
        trades = await s.scalar(select(func.count()).select_from(TradeRow))
    assert trades == 0
    assert sig is not None and not sig.acted and sig.reject_reason == "halted:kill_switch"


def test_paper_mode_refuses_mainnet_keys() -> None:
    from pydantic import SecretStr

    config = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml")
    settings = Settings(
        _env_file=None,
        mode=RunMode.PAPER,
        bybit_testnet=False,
        bybit_api_key=SecretStr("k"),
        bybit_api_secret=SecretStr("s"),
    )
    runtime = BotRuntime(settings, config, EventBus())
    with pytest.raises(RuntimeError, match="TESTNET"):
        runtime._make_broker(config.markets["crypto"])
    live = Settings(
        _env_file=None, mode=RunMode.LIVE, bybit_testnet=False, jwt_secret=SecretStr("j" * 32)
    )
    with pytest.raises(RuntimeError, match="live"):
        BotRuntime(live, config, EventBus())._make_broker(config.markets["crypto"])
