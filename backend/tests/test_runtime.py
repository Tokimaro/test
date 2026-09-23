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
from tests.synthetic import frame_to_candles, make_ohlcv, resample

pytestmark = pytest.mark.db


def setup_runtime(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> tuple[BotRuntime, EventBus]:
    raw = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml").model_dump(mode="json")
    raw["markets"] = {"crypto": {**raw["markets"]["crypto"], "symbols": ["BTCUSDT"]}}
    config = TradingConfig.from_dict(raw)

    now = wall_clock_ms()
    h1_ms = Timeframe.H1.ms
    start = (now // h1_ms - 2600) * h1_ms
    h1 = make_ohlcv(2600, vol=0.006, seed=1, start_ts=start)
    m15 = make_ohlcv(2600 * 4, vol=0.003, seed=2, start_ts=start, tf=Timeframe.M15)
    fake = FakeMarketBroker(
        {
            ("BTCUSDT", Timeframe.H1): frame_to_candles(h1),
            ("BTCUSDT", Timeframe.H4): frame_to_candles(resample(h1, Timeframe.H1, Timeframe.H4)),
            ("BTCUSDT", Timeframe.M15): frame_to_candles(m15),
        }
    )
    last = frame_to_candles(h1)[-1]
    fake.events = [CandleClosed("BTCUSDT", Timeframe.H1, last)]
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
    assert candles is not None and candles > 2500
    assert signals is not None and signals >= 1
    types = set()
    while not q.empty():
        types.add(q.get_nowait().type)
    assert {"bot_status", "equity", "signal"} <= types
    repo_state = await runtime.repo.get_state("paper_broker:crypto")
    assert repo_state is not None and Decimal(repo_state["cash"]) == Decimal(5_000)


async def test_halted_state_is_loaded_before_first_candle(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    migrated_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия: свежая свеча при старте не должна обходить сохранённый kill switch."""
    from app.analysis.regime import Regime
    from app.domain import Direction
    from app.risk.manager import RiskManager, RiskState
    from app.strategy.ensemble import Signal, SignalEngine

    runtime, _ = setup_runtime(migrated_db, monkeypatch)
    halted = RiskManager(
        runtime.config.risk, state=RiskState(halted=True, halt_reason="kill_switch")
    )
    await runtime.repo.set_state("risk", halted.to_dict())

    def always_long(
        self: SignalEngine, prepared: object, symbol: str, ctx: object = None
    ) -> Signal:
        return Signal(0, symbol, Direction.LONG, 99.0, Regime.TREND_UP, "trend")

    monkeypatch.setattr(SignalEngine, "evaluate_last", always_long)
    await runtime.start()
    await asyncio.sleep(0.3)
    await runtime.stop()
    async with db_sessionmaker() as s:
        sig = await s.scalar(select(SignalRow))
        trades = await s.scalar(select(func.count()).select_from(TradeRow))
    assert trades == 0
    assert sig is not None and sig.reject_reason == "halted:kill_switch"
