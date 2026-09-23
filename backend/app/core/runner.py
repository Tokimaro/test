"""Сборка и запуск бота: адаптеры, хранилище, поток свечей, движок, фоновые задачи.

Режимы:
* paper без ключей API → PaperBroker поверх публичных данных Bybit mainnet;
* paper с ключами и TB_BYBIT_TESTNET=true → настоящий testnet Bybit;
* live → Bybit mainnet (требует ключей, см. Settings).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.brokers.base import BrokerAdapter
from app.brokers.bybit.adapter import BybitAdapter
from app.brokers.bybit.client import BybitHttpClient
from app.brokers.paper import PaperBroker
from app.config import RunMode, Settings
from app.core.engine import TradingEngine
from app.core.events import EventBus
from app.db.candles import SqlCandleStore
from app.db.repo import TradeRepo
from app.db.session import make_engine, make_sessionmaker
from app.domain import MarketType, Timeframe
from app.market.feed import CandleFeed
from app.trading_config import MarketSettings, TradingConfig

log = structlog.get_logger()

RECONCILE_INTERVAL_S = 30
PAPER_STATE_KEY = "paper_broker"
TRADING_CONFIG_KEY = "trading_config"


class BotRuntime:
    def __init__(
        self,
        settings: Settings,
        config: TradingConfig,
        bus: EventBus,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings
        self.config = config
        self.bus = bus
        # собственное подключение к БД — только если приложение не передало общее
        self.db: AsyncEngine | None = None
        if sessionmaker is None:
            self.db = make_engine(settings.database_url)
            sessionmaker = make_sessionmaker(self.db)
        self.sm = sessionmaker
        self.repo = TradeRepo(self.sm, settings.mode.value)
        self.store = SqlCandleStore(self.sm, broker="bybit")
        self.brokers: dict[str, BrokerAdapter] = {}
        self.data_brokers: dict[str, BrokerAdapter] = {}
        self.feeds: dict[str, CandleFeed] = {}
        self.engine: TradingEngine | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def _make_broker(self, market: MarketSettings) -> tuple[BrokerAdapter, BrokerAdapter]:
        """Возвращает (торговый брокер, источник данных)."""
        s = self.settings
        key = s.bybit_api_key.get_secret_value()
        secret = s.bybit_api_secret.get_secret_value()
        market_type = MarketType(market.market_type)
        if s.mode is RunMode.PAPER and not (key and secret):
            data = BybitAdapter(
                BybitHttpClient(testnet=False),
                category=market.category,
                market_type=market_type,
                testnet=False,
            )
            return PaperBroker(data, initial_equity=s.paper_initial_equity), data
        client = BybitHttpClient(
            testnet=s.bybit_testnet,
            api_key=key,
            api_secret=secret,
            recv_window_ms=s.bybit_recv_window_ms,
        )
        adapter = BybitAdapter(
            client, category=market.category, market_type=market_type, testnet=s.bybit_testnet
        )
        return adapter, adapter

    async def start(self) -> None:
        saved = await self.repo.get_setting(TRADING_CONFIG_KEY)
        if saved:
            try:
                self.config = TradingConfig.from_dict(saved)
                log.info("runtime.config_from_db")
            except ValueError as exc:
                log.error("runtime.saved_config_invalid", error=str(exc))
        instrument_ids: dict[str, int] = {}
        for name, market in self.config.markets.items():
            if not market.enabled or not market.symbols:
                continue
            broker, data = self._make_broker(market)
            self.brokers[name], self.data_brokers[name] = broker, data
            if isinstance(broker, PaperBroker):
                state = await self.repo.get_state(f"{PAPER_STATE_KEY}:{name}")
                if state:
                    broker.load_dict(state)
            for symbol in market.symbols:
                instrument_ids[symbol] = await self.store.register(
                    await data.get_instrument(symbol)
                )

        engine = TradingEngine(
            config=self.config,
            brokers=self.brokers,
            store=self.store,
            repo=self.repo,
            bus=self.bus,
            instrument_ids=instrument_ids,
            ensure_fresh=self._ensure_fresh,
        )
        self.engine = engine
        # Сначала восстановить состояние (открытые сделки, риск, kill switch), и только потом
        # пускать свечи: иначе свежая свеча могла бы открыть сделку «вслепую»
        await engine.start()
        for name, broker in self.brokers.items():
            market = self.config.markets[name]
            tfs = market.timeframes
            subs = [(s, tf) for s in market.symbols for tf in (tfs.entry, tfs.working, tfs.higher)]
            self.feeds[name] = CandleFeed(
                broker, self.store, subs, engine.on_candle, on_synced=engine.on_backfill
            )
            await self.feeds[name].sync_all()
        for name, feed in self.feeds.items():
            self._tasks.append(asyncio.create_task(self._supervise(f"feed:{name}", feed.run)))
        self._tasks.append(asyncio.create_task(self._supervise("reconcile", self._reconcile_loop)))
        log.info("runtime.started", mode=self.settings.mode, markets=list(self.brokers))

    async def _ensure_fresh(self, symbol: str, tf: Timeframe) -> None:
        for name, market in self.config.markets.items():
            if symbol in market.symbols and name in self.feeds:
                await self.feeds[name].sync(symbol, tf)

    async def _reconcile_loop(self) -> None:
        assert self.engine is not None
        while True:
            await self.engine.reconcile()
            await self._save_paper_state()
            await asyncio.sleep(RECONCILE_INTERVAL_S)

    async def _save_paper_state(self) -> None:
        for name, broker in self.brokers.items():
            if isinstance(broker, PaperBroker):
                await self.repo.set_state(f"{PAPER_STATE_KEY}:{name}", broker.to_dict())

    async def _supervise(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Перезапускает упавшую фоновую задачу с паузой, сообщая об ошибке."""
        while True:
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("runtime.task_failed", task=name)
                self.bus.publish(
                    "alert", level="error", kind="task_failed", task=name, error=str(exc)
                )
                await asyncio.sleep(5)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self._save_paper_state()
        # PaperBroker.aclose закрывает свой источник данных; для live брокер и есть источник
        for broker in {id(b): b for b in self.brokers.values()}.values():
            await broker.aclose()
        if self.db is not None:
            await self.db.dispose()
        log.info("runtime.stopped")
