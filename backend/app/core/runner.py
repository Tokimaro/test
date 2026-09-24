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

from app.brokers.alpaca import DATA_URL as ALPACA_DATA_URL
from app.brokers.alpaca import LIVE_URL as ALPACA_LIVE_URL
from app.brokers.alpaca import PAPER_URL as ALPACA_PAPER_URL
from app.brokers.alpaca import AlpacaAdapter, AlpacaClient
from app.brokers.base import BrokerAdapter, BrokerError
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
from app.market.store import CandleStore, RoutingCandleStore
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
        self.store: CandleStore = RoutingCandleStore({})
        self.brokers: dict[str, BrokerAdapter] = {}
        self.data_brokers: dict[str, BrokerAdapter] = {}
        self.feeds: dict[str, CandleFeed] = {}
        self.engine: TradingEngine | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def _make_broker(self, market: MarketSettings) -> tuple[BrokerAdapter, BrokerAdapter] | None:
        """Возвращает (торговый брокер, источник данных) или None, если рынок недоступен."""
        if market.broker == "alpaca":
            return self._make_alpaca(market)
        s = self.settings
        key = s.bybit_api_key.get_secret_value()
        secret = s.bybit_api_secret.get_secret_value()
        market_type = MarketType(market.market_type)
        has_keys = bool(key and secret)
        if s.mode is RunMode.PAPER and has_keys and not s.bybit_testnet:
            # иначе «paper»-режим отправлял бы настоящие ордера на mainnet
            raise RuntimeError(
                "в режиме paper ключи Bybit допустимы только с TB_BYBIT_TESTNET=true"
            )
        if s.mode is RunMode.LIVE and not has_keys:
            raise RuntimeError("режим live требует ключей Bybit (окружение или панель)")
        if s.mode is RunMode.PAPER and not has_keys:
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

    def _make_alpaca(self, market: MarketSettings) -> tuple[BrokerAdapter, BrokerAdapter] | None:
        s = self.settings
        key = s.alpaca_api_key.get_secret_value()
        secret = s.alpaca_api_secret.get_secret_value()
        if not (key and secret):
            log.warning("runtime.alpaca_keys_missing", hint="задайте TB_ALPACA_API_KEY/SECRET")
            return None
        # paper-режим бота всегда торгует на paper-счёте Alpaca
        paper = s.mode is not RunMode.LIVE or s.alpaca_paper
        adapter = AlpacaAdapter(
            AlpacaClient(ALPACA_PAPER_URL if paper else ALPACA_LIVE_URL, key, secret),
            AlpacaClient(ALPACA_DATA_URL, key, secret),
            paper=paper,
            feed=s.alpaca_feed,
            session_buffer_minutes=15,
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
        routes: dict[str, CandleStore] = {}
        for name, market in self.config.markets.items():
            if not market.enabled or not market.symbols:
                continue
            made = self._make_broker(market)
            if made is None:
                self.bus.publish("alert", level="warning", kind="market_unavailable", market=name)
                continue
            broker, data = made
            store = SqlCandleStore(self.sm, broker=market.broker)
            try:
                ids = {
                    s: await store.register(await data.get_instrument(s)) for s in market.symbols
                }
            except BrokerError as exc:
                # один недоступный рынок не должен останавливать остальные
                log.error("runtime.market_init_failed", market=name, error=str(exc))
                self.bus.publish(
                    "alert", level="error", kind="market_unavailable", market=name, error=str(exc)
                )
                await broker.aclose()
                continue
            if isinstance(broker, PaperBroker):
                state = await self.repo.get_state(f"{PAPER_STATE_KEY}:{name}")
                if state:
                    broker.load_dict(state)
            self.brokers[name], self.data_brokers[name] = broker, data
            instrument_ids |= ids
            routes |= dict.fromkeys(market.symbols, store)
        self.store = RoutingCandleStore(routes)

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
            subs = [(s, Timeframe.D1) for s in market.symbols]
            self.feeds[name] = CandleFeed(
                broker, self.store, subs, engine.on_candle, on_synced=engine.on_backfill
            )
            try:
                await self.feeds[name].sync_all()
            except BrokerError as exc:
                # поток свечей сам повторит загрузку; остальные рынки продолжают работать
                log.error("runtime.initial_sync_failed", market=name, error=str(exc))
                self.bus.publish(
                    "alert", level="error", kind="history_sync_failed", market=name, error=str(exc)
                )
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
